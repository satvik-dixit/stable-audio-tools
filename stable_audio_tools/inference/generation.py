import numpy as np
import torch 
import typing as tp
import math 
from torchaudio import transforms as T

from .utils import prepare_audio
from .sampling import sample, sample_k, sample_rf
from ..data.utils import PadCrop

def generate_diffusion_uncond(
        model,
        steps: int = 250,
        batch_size: int = 1,
        sample_size: int = 2097152,
        seed: int = -1,
        device: str = "cuda",
        init_audio: tp.Optional[tp.Tuple[int, torch.Tensor]] = None,
        init_noise_level: float = 1.0,
        return_latents = False,
        **sampler_kwargs
        ) -> torch.Tensor:
    
    # The length of the output in audio samples 
    audio_sample_size = sample_size

    # If this is latent diffusion, change sample_size instead to the downsampled latent size
    if model.pretransform is not None:
        sample_size = sample_size // model.pretransform.downsampling_ratio
        
    # Seed
    # The user can explicitly set the seed to deterministically generate the same output. Otherwise, use a random seed.
    seed = seed if seed != -1 else np.random.randint(0, 2**32 - 1, dtype=np.uint32)
    print(seed)
    torch.manual_seed(seed)
    # Define the initial noise immediately after setting the seed
    noise = torch.randn([batch_size, model.io_channels, sample_size], device=device)

    if init_audio is not None:
        # The user supplied some initial audio (for inpainting or variation). Let us prepare the input audio.
        in_sr, init_audio = init_audio

        io_channels = model.io_channels

        # For latent models, set the io_channels to the autoencoder's io_channels
        if model.pretransform is not None:
            io_channels = model.pretransform.io_channels

        # Prepare the initial audio for use by the model
        init_audio = prepare_audio(init_audio, in_sr=in_sr, target_sr=model.sample_rate, target_length=audio_sample_size, target_channels=io_channels, device=device)

        # For latent models, encode the initial audio into latents
        if model.pretransform is not None:
            init_audio = model.pretransform.encode(init_audio)

        init_audio = init_audio.repeat(batch_size, 1, 1)
    else:
        # The user did not supply any initial audio for inpainting or variation. Generate new output from scratch. 
        init_audio = None
        init_noise_level = None

    # Inpainting mask
    
    if init_audio is not None:
        # variations
        sampler_kwargs["sigma_max"] = init_noise_level
        mask = None 
    else:
        mask = None

    # Now the generative AI part:

    diff_objective = model.diffusion_objective

    if diff_objective == "v":    
        # k-diffusion denoising process go!
        sampled = sample_k(model.model, noise, init_audio, mask, steps, **sampler_kwargs, device=device)
    elif diff_objective == "rectified_flow":
        sampled = sample_rf(model.model, noise, init_data=init_audio, steps=steps, **sampler_kwargs, device=device)

    # Denoising process done. 
    # If this is latent diffusion, decode latents back into audio
    if model.pretransform is not None and not return_latents:
        sampled = model.pretransform.decode(sampled)

    # Return audio
    return sampled

# def get_morphed_embedding(weights, audio_embed):

#     weights = torch.tensor(weights, dtype=torch.float32)
#     weights = weights.to('cuda')
#     weights = weights / weights.sum()

#     weights = weights.view(-1, 1)
#     audio_embed_tensor = torch.stack(audio_embed, dim=0).squeeze()  # Shape: [n, 512]
#     audio_embed_tensor = audio_embed_tensor.to('cuda')
#     morphed_embedding = torch.sum(weights * audio_embed_tensor, dim=0)

#     return morphed_embedding


def apply_conditioning(tensors_list, weights_list):
    # Extract the prompts and masks
    prompts = [entry['prompt'][0] for entry in tensors_list]
    masks = [entry['prompt'][1] for entry in tensors_list]

    # Convert weights_list to a tensor
    weights = torch.tensor(weights_list, device=prompts[0].device)

    # Compute the weighted mean of the prompts
    weighted_prompt = torch.stack(prompts) * weights[:, None, None, None]
        
    weighted_prompt = weighted_prompt.sum(dim=0)

    # Apply the mask from the first element
    mask = masks[0]
    # weighted_prompt = weighted_prompt * mask

    # Retrieve everything else from the first element
    conditioned_tensors = {
        'prompt': (weighted_prompt, mask),
        'seconds_start': tensors_list[0]['seconds_start'],
        'seconds_total': tensors_list[0]['seconds_total']
    }

    return conditioned_tensors

def generate_diffusion_cond(
        model,
        steps: int = 250,
        cfg_scale=6,
        conditioning_list: list = None,  # List of conditioning dicts
        weights_list: list = None,        # List of weights
        conditioning_tensors: tp.Optional[dict] = None,
        negative_conditioning: dict = None,
        negative_conditioning_tensors: tp.Optional[dict] = None,
        batch_size: int = 1,
        sample_size: int = 2097152,
        sample_rate: int = 48000,
        seed: int = -1,
        device: str = "cuda",
        init_audio: tp.Optional[tp.Tuple[int, torch.Tensor]] = None,
        init_noise_level: float = 1.0,
        mask_args: dict = None,
        return_latents=False,
        **sampler_kwargs
        ) -> torch.Tensor:
    # print('condit function called')

    # The length of the output in audio samples 
    audio_sample_size = sample_size

    # If this is latent diffusion, change sample_size instead to the downsampled latent size
    if model.pretransform is not None:
        sample_size = sample_size // model.pretransform.downsampling_ratio

    # Seed
    seed = seed if seed != -1 else np.random.randint(0, 2**32 - 1, dtype=np.uint32)
    # print(seed)
    torch.manual_seed(seed)

    # Define the initial noise
    noise = torch.randn([batch_size, model.io_channels, sample_size], device=device)

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    torch.backends.cudnn.benchmark = False

    # Conditioning
    assert conditioning_list is not None or conditioning_tensors is not None, "Must provide either conditioning_list or conditioning_tensors"
    
    if conditioning_tensors is None:
        conditioning_tensors_list = [model.conditioner(cond, device) for cond in conditioning_list]
        # print('conditioning_tensors_list', conditioning_tensors_list)
        conditioning_tensors = apply_conditioning(conditioning_tensors_list, weights_list)
        # print('conditioning_tensors', conditioning_tensors)

        # # Get the cross_attn_cond tensors from all conditionings and compute the weighted mean
        # cross_attn_conds = [model.get_conditioning_inputs(cond_tensors)['cross_attn_cond'] for cond_tensors in conditioning_tensors_list]
        # weighted_cross_attn_cond = get_morphed_embedding(weights_list, cross_attn_conds)

        # # Use the weighted mean as the new cross_attn_cond
        # conditioning_tensors = model.get_conditioning_inputs(conditioning_tensors_list[0])  # Use one of the conditioning inputs as a template
        # conditioning_tensors['cross_attn_cond'] = weighted_cross_attn_cond

    conditioning_inputs = model.get_conditioning_inputs(conditioning_tensors)


    if negative_conditioning is not None or negative_conditioning_tensors is not None:
        if negative_conditioning_tensors is None:
            negative_conditioning_tensors = model.conditioner(negative_conditioning, device)
        negative_conditioning_tensors = model.get_conditioning_inputs(negative_conditioning_tensors, negative=True)
    else:
        negative_conditioning_tensors = {}

    if init_audio is not None:
        in_sr, init_audio = init_audio
        io_channels = model.io_channels

        if model.pretransform is not None:
            io_channels = model.pretransform.io_channels

        init_audio = prepare_audio(init_audio, in_sr=in_sr, target_sr=model.sample_rate, target_length=audio_sample_size, target_channels=io_channels, device=device)

        if model.pretransform is not None:
            init_audio = model.pretransform.encode(init_audio)

        init_audio = init_audio.repeat(batch_size, 1, 1)
    else:
        init_audio = None
        init_noise_level = None
        mask_args = None

    if init_audio is not None and mask_args is not None:
        cropfrom = math.floor(mask_args["cropfrom"] / 100.0 * sample_size)
        pastefrom = math.floor(mask_args["pastefrom"] / 100.0 * sample_size)
        pasteto = math.ceil(mask_args["pasteto"] / 100.0 * sample_size)
        assert pastefrom < pasteto, "Paste From should be less than Paste To"
        croplen = pasteto - pastefrom
        if cropfrom + croplen > sample_size:
            croplen = sample_size - cropfrom 
        cropto = cropfrom + croplen
        pasteto = pastefrom + croplen
        cutpaste = init_audio.new_zeros(init_audio.shape)
        cutpaste[:, :, pastefrom:pasteto] = init_audio[:,:,cropfrom:cropto]
        init_audio = cutpaste
        mask = build_mask(sample_size, mask_args).to(device)
    elif init_audio is not None and mask_args is None:
        sampler_kwargs["sigma_max"] = init_noise_level
        mask = None 
    else:
        mask = None

    model_dtype = next(model.model.parameters()).dtype
    noise = noise.type(model_dtype)
    conditioning_inputs = {k: v.type(model_dtype) if v is not None else v for k, v in conditioning_inputs.items()}
    # print('conditioning_inputs', conditioning_inputs)

    diff_objective = model.diffusion_objective

    if diff_objective == "v":    
        sampled = sample_k(model.model, noise, init_audio, mask, steps, **sampler_kwargs, **conditioning_inputs, **negative_conditioning_tensors, cfg_scale=cfg_scale, batch_cfg=True, rescale_cfg=True, device=device)
    elif diff_objective == "rectified_flow":
        if "sigma_min" in sampler_kwargs:
            del sampler_kwargs["sigma_min"]

        if "sampler_type" in sampler_kwargs:
            del sampler_kwargs["sampler_type"]

        sampled = sample_rf(model.model, noise, init_data=init_audio, steps=steps, **sampler_kwargs, **conditioning_inputs, **negative_conditioning_tensors, cfg_scale=cfg_scale, batch_cfg=True, rescale_cfg=True, device=device)

    del noise
    del conditioning_tensors
    del conditioning_inputs
    torch.cuda.empty_cache()

    if model.pretransform is not None and not return_latents:
        sampled = sampled.to(next(model.pretransform.parameters()).dtype)
        sampled = model.pretransform.decode(sampled)

    return sampled

# def generate_diffusion_cond(
#         model,
#         steps: int = 250,
#         cfg_scale=6,
#         conditioning: dict = None,
#         conditioning_tensors: tp.Optional[dict] = None,
#         negative_conditioning: dict = None,
#         negative_conditioning_tensors: tp.Optional[dict] = None,
#         batch_size: int = 1,
#         sample_size: int = 2097152,
#         sample_rate: int = 48000,
#         seed: int = -1,
#         device: str = "cuda",
#         init_audio: tp.Optional[tp.Tuple[int, torch.Tensor]] = None,
#         init_noise_level: float = 1.0,
#         mask_args: dict = None,
#         return_latents = False,
#         **sampler_kwargs
#         ) -> torch.Tensor: 
#     """
#     Generate audio from a prompt using a diffusion model.
    
#     Args:
#         model: The diffusion model to use for generation.
#         steps: The number of diffusion steps to use.
#         cfg_scale: Classifier-free guidance scale 
#         conditioning: A dictionary of conditioning parameters to use for generation.
#         conditioning_tensors: A dictionary of precomputed conditioning tensors to use for generation.
#         batch_size: The batch size to use for generation.
#         sample_size: The length of the audio to generate, in samples.
#         sample_rate: The sample rate of the audio to generate (Deprecated, now pulled from the model directly)
#         seed: The random seed to use for generation, or -1 to use a random seed.
#         device: The device to use for generation.
#         init_audio: A tuple of (sample_rate, audio) to use as the initial audio for generation.
#         init_noise_level: The noise level to use when generating from an initial audio sample.
#         return_latents: Whether to return the latents used for generation instead of the decoded audio.
#         **sampler_kwargs: Additional keyword arguments to pass to the sampler.    
#     """

#     # The length of the output in audio samples 
#     audio_sample_size = sample_size

#     # If this is latent diffusion, change sample_size instead to the downsampled latent size
#     if model.pretransform is not None:
#         sample_size = sample_size // model.pretransform.downsampling_ratio
        
#     # Seed
#     # The user can explicitly set the seed to deterministically generate the same output. Otherwise, use a random seed.
#     seed = seed if seed != -1 else np.random.randint(0, 2**32 - 1, dtype=np.uint32)
#     print(seed)
#     torch.manual_seed(seed)
#     # Define the initial noise immediately after setting the seed
#     noise = torch.randn([batch_size, model.io_channels, sample_size], device=device)

#     torch.backends.cuda.matmul.allow_tf32 = False
#     torch.backends.cudnn.allow_tf32 = False
#     torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
#     torch.backends.cudnn.benchmark = False

#     # Conditioning
#     assert conditioning is not None or conditioning_tensors is not None, "Must provide either conditioning or conditioning_tensors"
#     if conditioning_tensors is None:
#         conditioning_tensors = model.conditioner(conditioning, device)
#     conditioning_inputs = model.get_conditioning_inputs(conditioning_tensors)

#     if negative_conditioning is not None or negative_conditioning_tensors is not None:
        
#         if negative_conditioning_tensors is None:
#             negative_conditioning_tensors = model.conditioner(negative_conditioning, device)
            
#         negative_conditioning_tensors = model.get_conditioning_inputs(negative_conditioning_tensors, negative=True)
#     else:
#         negative_conditioning_tensors = {}

#     if init_audio is not None:
#         # The user supplied some initial audio (for inpainting or variation). Let us prepare the input audio.
#         in_sr, init_audio = init_audio

#         io_channels = model.io_channels

#         # For latent models, set the io_channels to the autoencoder's io_channels
#         if model.pretransform is not None:
#             io_channels = model.pretransform.io_channels

#         # Prepare the initial audio for use by the model
#         init_audio = prepare_audio(init_audio, in_sr=in_sr, target_sr=model.sample_rate, target_length=audio_sample_size, target_channels=io_channels, device=device)

#         # For latent models, encode the initial audio into latents
#         if model.pretransform is not None:
#             init_audio = model.pretransform.encode(init_audio)

#         init_audio = init_audio.repeat(batch_size, 1, 1)
#     else:
#         # The user did not supply any initial audio for inpainting or variation. Generate new output from scratch. 
#         init_audio = None
#         init_noise_level = None
#         mask_args = None

#     # Inpainting mask
#     if init_audio is not None and mask_args is not None:
#         # Cut and paste init_audio according to cropfrom, pastefrom, pasteto
#         # This is helpful for forward and reverse outpainting
#         cropfrom = math.floor(mask_args["cropfrom"]/100.0 * sample_size)
#         pastefrom = math.floor(mask_args["pastefrom"]/100.0 * sample_size)
#         pasteto = math.ceil(mask_args["pasteto"]/100.0 * sample_size)
#         assert pastefrom < pasteto, "Paste From should be less than Paste To"
#         croplen = pasteto - pastefrom
#         if cropfrom + croplen > sample_size:
#             croplen = sample_size - cropfrom 
#         cropto = cropfrom + croplen
#         pasteto = pastefrom + croplen
#         cutpaste = init_audio.new_zeros(init_audio.shape)
#         cutpaste[:, :, pastefrom:pasteto] = init_audio[:,:,cropfrom:cropto]
#         #print(cropfrom, cropto, pastefrom, pasteto)
#         init_audio = cutpaste
#         # Build a soft mask (list of floats 0 to 1, the size of the latent) from the given args
#         mask = build_mask(sample_size, mask_args)
#         mask = mask.to(device)
#     elif init_audio is not None and mask_args is None:
#         # variations
#         sampler_kwargs["sigma_max"] = init_noise_level
#         mask = None 
#     else:
#         mask = None

#     model_dtype = next(model.model.parameters()).dtype
#     noise = noise.type(model_dtype)
#     conditioning_inputs = {k: v.type(model_dtype) if v is not None else v for k, v in conditioning_inputs.items()}
#     # Now the generative AI part:
#     # k-diffusion denoising process go!

#     diff_objective = model.diffusion_objective

#     if diff_objective == "v":    
#         # k-diffusion denoising process go!
#         sampled = sample_k(model.model, noise, init_audio, mask, steps, **sampler_kwargs, **conditioning_inputs, **negative_conditioning_tensors, cfg_scale=cfg_scale, batch_cfg=True, rescale_cfg=True, device=device)
#     elif diff_objective == "rectified_flow":

#         if "sigma_min" in sampler_kwargs:
#             del sampler_kwargs["sigma_min"]

#         if "sampler_type" in sampler_kwargs:
#             del sampler_kwargs["sampler_type"]

#         sampled = sample_rf(model.model, noise, init_data=init_audio, steps=steps, **sampler_kwargs, **conditioning_inputs, **negative_conditioning_tensors, cfg_scale=cfg_scale, batch_cfg=True, rescale_cfg=True, device=device)

#     # v-diffusion: 
#     #sampled = sample(model.model, noise, steps, 0, **conditioning_tensors, embedding_scale=cfg_scale)
#     del noise
#     del conditioning_tensors
#     del conditioning_inputs
#     torch.cuda.empty_cache()
#     # Denoising process done. 
#     # If this is latent diffusion, decode latents back into audio
#     if model.pretransform is not None and not return_latents:
#         #cast sampled latents to pretransform dtype
#         sampled = sampled.to(next(model.pretransform.parameters()).dtype)
#         sampled = model.pretransform.decode(sampled)

#     # Return audio
#     return sampled

# builds a softmask given the parameters
# returns array of values 0 to 1, size sample_size, where 0 means noise / fresh generation, 1 means keep the input audio, 
# and anything between is a mixture of old/new
# ideally 0.5 is half/half mixture but i haven't figured this out yet
def build_mask(sample_size, mask_args):
    maskstart = math.floor(mask_args["maskstart"]/100.0 * sample_size)
    maskend = math.ceil(mask_args["maskend"]/100.0 * sample_size)
    softnessL = round(mask_args["softnessL"]/100.0 * sample_size)
    softnessR = round(mask_args["softnessR"]/100.0 * sample_size)
    marination = mask_args["marination"]
    # use hann windows for softening the transition (i don't know if this is correct)
    hannL = torch.hann_window(softnessL*2, periodic=False)[:softnessL]
    hannR = torch.hann_window(softnessR*2, periodic=False)[softnessR:]
    # build the mask. 
    mask = torch.zeros((sample_size))
    mask[maskstart:maskend] = 1
    mask[maskstart:maskstart+softnessL] = hannL
    mask[maskend-softnessR:maskend] = hannR
    # marination finishes the inpainting early in the denoising schedule, and lets audio get changed in the final rounds
    if marination > 0:        
        mask = mask * (1-marination) 
    #print(mask)
    return mask
