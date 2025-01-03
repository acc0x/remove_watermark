import numpy as np
import einops
from rp import *

try:
    import torch
except ImportError:
    pass

__all__ = ["remove_watermark", "demo_remove_watermark"]


def _is_uint8(x):
    if   is_numpy_array (x): return x.dtype == np.uint8
    elif is_torch_tensor(x): return x.dtype == torch.uint8
    else: raise TypeError(f"Unsupported input type: {type(x)}")


def _fft2(x):
    if   is_numpy_array (x): return    np.fft.fft2(x)
    elif is_torch_tensor(x): return torch.fft.fft2(x)
    else: raise TypeError(f"Unsupported input type: {type(x)}")


def _ifft2(x):
    if   is_numpy_array (x): return    np.fft.ifft2(x)
    elif is_torch_tensor(x): return torch.fft.ifft2(x)
    else: raise TypeError(f"Unsupported input type: {type(x)}")


def _fftshift(x):
    if   is_numpy_array (x): return    np.fft.fftshift(x)
    elif is_torch_tensor(x): return torch.fft.fftshift(x)
    else: raise TypeError(f"Unsupported input type: {type(x)}")


def _clip(x, min_val, max_val):
    if   is_numpy_array (x): return    np.clip (x, min_val, max_val)
    elif is_torch_tensor(x): return torch.clamp(x, min_val, max_val)
    else: raise TypeError(f"Unsupported input type: {type(x)}")


def _roll(x, shift, dims):
    if   is_numpy_array (x): return    np.roll(x, shift, axis=dims)
    elif is_torch_tensor(x): return torch.roll(x, shift, dims=dims)
    else: raise TypeError(f"Unsupported input type: {type(x)}")

def _default_form(x):
    if   is_numpy_array (x): return "THWC"
    elif is_torch_tensor(x): return "TCHW"
    else: raise TypeError(f"Unsupported input type: {type(x)}")

def _like(x, target):
    if   is_numpy_array (x) and is_numpy_array (target): return x
    elif is_torch_tensor(x) and is_torch_tensor(target): return x
    elif is_torch_tensor(x) and is_numpy_array (target): return as_numpy_array(x)
    elif is_numpy_array (x) and is_torch_tensor(target): return torch.tensor(x).to(target.device, target.dtype)
    else: raise TypeError(f"Unsupported input types: {type(x)} {type(target)}")

@memoized
def _get_watermark_image():
    watermark_path = with_file_name(__file__, "watermark.exr")
    watermark = load_image(watermark_path, use_cache=True)
    assert is_rgba_image(watermark), "Without alpha, the watermark is useless"
    assert is_float_image(watermark), "Watermark should ideally be saved with floating-point precision"
    return watermark


def remove_watermark(video, form=None):
    """Removes watermark from a video.

    Given an RGB video as a THWC NumPy array in THWC form or TCHW PyTorch tensor, where T is num_frames,
    H and W are height and width, and 3 (channels) is for RGB. It assumes
    it's a watermarked video - matching the watermark found in watermark.exr
    (in the same folder as this python file). Currently, that watermark is
    for shutterstock videos - and is created with make_watermark_exr.py,
    also found in the same folder as this python file.

    Args:
        video: A NumPy array or PyTorch tensor representing the video frames in THW3 format.
        form (str, optional): If you want to use numpy videos in TCHW form or torch videos in THWC form, specify that.
            Valid options are 'TCHW' and 'THWC'

    Returns:
        A NumPy array or PyTorch tensor of the same shape and type as the input video, with the
        watermark removed, and floating point pixel values between 0 and 1.

    Notes:
        The function works by:
        1. Convolving the RGBA watermark over the mean of all frames in
           grayscale to locate the watermark position. This uses FFT and
           IFFT for speed. (Technically uses cross-correlation)
        2. Once the watermark shift is found, it does inverse alpha-blending
           to remove the watermark from all frames.

        The complexity is O(total num pixels in video) aka O(B * H * W).
        It is very fast and robust, even working on videos with the watermark
        upside-down.
    """

    if form is None:
        form = _default_form(video)
    assert form in ['TCHW', 'THWC']
    if form=='TCHW':
        video     = einops.rearrange(video,     'T C H W -> T H W C')
        recovered = remove_watermark(video, form = 'THWC')
        recovered = einops.rearrange(recovered, 'T H W C -> T C H W')
        return recovered

    def recover_background(composite_images, rgba_watermark):
        # Extract RGB and Alpha components of the watermark
        watermark_rgb   = rgba_watermark[:, :, :3]
        watermark_alpha = rgba_watermark[:, :, 3:]

        # Calculate the background image using the derived formula
        # Use _clip to ensure the resulting pixel values are still in the range [0, 1]
        background = (composite_images - watermark_alpha * watermark_rgb) / (1 - watermark_alpha)
        background = _clip(background, 0, 1)

        return background

    def get_shifts():
        def cross_corr(img1, img2):
            assert is_a_matrix(img1)
            assert is_a_matrix(img2)

            # Compute the FFT of both images
            fft1 = _fft2(img1)
            fft2 = _fft2(img2)
            # Compute the cross-correlation in frequency domain
            cross_fft = fft1 * fft2.conj()
            # Compute the inverse FFT to get the cross-correlation in spatial domain
            cross_corr = _ifft2(cross_fft)
            # Shift the zero-frequency component to the center of the spectrum
            cross_corr = _fftshift(cross_corr)
            return cross_corr.real

        def best_shift(frame, watermark):
            # Compute the cross-correlation between frame and watermark
            corr = cross_corr(frame, watermark)
            # Find the coordinates of the maximum correlation
            max_loc = np.unravel_index(np.argmax(corr), corr.shape)
            # Compute the shift amounts
            dy, dx = (
                max_loc[0] - watermark.shape[0] // 2,
                max_loc[1] - watermark.shape[1] // 2,
            )
            return dx, dy

        #This function operates entirely in numpy. Don't worry, it's very fast!
        zavg_frame = as_numpy_array(avg_frame)
        zwatermark = as_numpy_array(watermark)
        zwatermark = blend_images(0.5, zwatermark) - 0.5  # Shape: H W C
        zavg_frame = zavg_frame - cv_gauss_blur(zavg_frame, sigma=20)  # Shape: H W C
        zavg_frame = as_grayscale_image(zavg_frame)
        zwatermark = as_grayscale_image(zwatermark)

        return best_shift(zavg_frame, zwatermark)

    if _is_uint8(video):
        video = video / 255

    avg_frame = video.mean(0)

    watermark = _get_watermark_image()

    # Make sure the watermark image is the same shape and type as the video so we can convolve them
    h, w, _ = avg_frame.shape
    watermark = crop_image(watermark, h, w)
    watermark = _like(watermark, avg_frame)


    best_watermark = None

    best_x_shift, best_y_shift = get_shifts()
    best_watermark = _roll(watermark, (best_y_shift, best_x_shift), dims=(0, 1))

    recovered = recover_background(video, best_watermark)

    return recovered


def demo_remove_watermark(input_video_glob="webvid/*.mp4", device=None):
    """Demonstrates the remove_watermark function on a set of videos.

    Applies remove_watermark to a set of videos specified by the given glob
    pattern, and saves watermark-removed versions to the 'output_videos/' directory.

    Args:
        input_video_glob: A glob pattern specifying the set of videos to process. Defaults to 'webvid/*.mp4'.
        device: If None, will use numpy. If a string like 'cpu' or 'cuda', will use torch.

    Notes:
        This demo function is fast enough to run on a typical laptop CPU.
        The processed videos are saved with filenames matching the input video names.
    """
    test_videos = rp_glob(input_video_glob)
    test_videos = shuffled(test_videos)

    while test_videos:
        video_path = test_videos.pop()

        fansi_print("Loading video from " + video_path, "green", "bold")
        tic()
        video = load_video(video_path, use_cache=False)
        video = as_numpy_array(video)  # Do not resize or truncate the video
        
        if device is not None:
            video = torch.tensor(video, device=device)
            fansi_print("Using pytorch on device = "+repr(device), 'green','bold')
        else:
            fansi_print("Using numpy on device = "+repr(device), 'magenta','bold')

        ptoctic()
        recovered = remove_watermark(video)
        ptoc()

        # Convert the recovered video back to numpy array if needed
        recovered = as_numpy_array(recovered)

        # Save the recovered video
        output_path = get_unique_copy_path(
            "output_videos/" + with_file_extension(
                get_file_name(video_path, include_file_extension=False), "mp4"
            )
        )
        save_video_mp4(recovered, output_path, framerate=30)
        fansi_print(f"Saved watermark-removed video at {output_path}", "green", "bold")


if __name__ == "__main__":
    # Example usage: Process all videos in the "webvid/" directory
    input_glob = "webvid/*.mp4"  # Specify the input video glob pattern
    output_device = None         # Set to 'cpu' or 'cuda' for PyTorch, or None for NumPy
    demo_remove_watermark(input_video_glob=input_glob, device=output_device)
