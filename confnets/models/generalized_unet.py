import torch
import torch.nn as nn
from copy import deepcopy

from ..blocks import SamePadResBlock
from ..layers import Identity, ConvNormActivation, UpsampleAndCrop, Crop
from .unet import EncoderDecoderSkeleton


# TODO: add subclass with one single output at highest depth

class MultiInputMultiOutputUNet(EncoderDecoderSkeleton):
    def __init__(self,
                 depth,
                 in_channels,
                 encoder_fmaps,
                 output_branches_specs,
                 decoder_fmaps=None,
                 number_multiscale_inputs=1,
                 scale_factor=2,
                 res_blocks_specs=None,
                 res_blocks_specs_decoder=None,
                 upsampling_mode='nearest',
                 decoder_crops=None,
                 return_input=False
                 ):
        """
        Generalized UNet model with the following features:

         - Attach one or more output-branches at any level of the UNet decoder for deep supervision
                (each branch requires the number of output channels and the final activation)
         - Optionally, pass multiple inputs at different scales. Features at different levels of
                the UNet encoder are auto-padded and concatenated to the given inputs.
         - Sum of skip-connections (no concatenation)
         - Downscale with strided conv
         - Optionally, perform spatial-crops in the UNet decoder to save memory and crop boundary artifacts
                (skip-connections are automatically cropped to match)
         - Custom number of 2D or 3D (same pad) ResBlocks at different levels of the UNet hierarchy


        TODO: Add example of output branches (both with list or dictionary, possibly two at the same depth)

        :param res_blocks_specs: None or list of booleans (length depth+1) specifying how many resBlocks we should concatenate
        at each level. Example:
            [
                [False, False], # Two 2D ResBlocks at the highest level
                [True],         # One 3D ResBlock at the second level of the UNet hierarchy
                [True, True],   # Two 3D ResBlock at the third level of the UNet hierarchy
            ]
        """
        assert isinstance(return_input, bool)
        self.return_input = return_input

        assert isinstance(depth, int)
        self.depth = depth

        assert isinstance(in_channels, int)
        self.in_channels = in_channels

        assert isinstance(upsampling_mode, str)
        self.upsampling_mode = upsampling_mode

        assert isinstance(number_multiscale_inputs, int)
        self.number_multiscale_inputs = number_multiscale_inputs

        def assert_depth_args(f_maps):
            assert isinstance(f_maps, (list, tuple))
            assert len(f_maps) == depth + 1

        # Parse feature maps:
        assert_depth_args(encoder_fmaps)
        self.encoder_fmaps = encoder_fmaps
        if decoder_fmaps is None:
            # By default use symmetric architecture:
            self.decoder_fmaps = encoder_fmaps
        else:
            assert_depth_args(decoder_fmaps)
            assert decoder_fmaps[-1] == encoder_fmaps[-1], "Number of layers at the base module should be the same"
            self.decoder_fmaps = decoder_fmaps

        # Parse scale factor:
        if isinstance(scale_factor, int):
            scale_factor = [scale_factor, ] * depth
        scale_factors = scale_factor
        normalized_factors = []
        for scale_factor in scale_factors:
            assert isinstance(scale_factor, (int, list, tuple))
            if isinstance(scale_factor, int):
                scale_factor = self.dim * [scale_factor]
            assert len(scale_factor) == self.dim
            normalized_factors.append(scale_factor)
        assert len(normalized_factors) == depth
        self.scale_factors = normalized_factors

        # Parse res-block specifications:
        if res_blocks_specs is None:
            # Default: one 3D block per level
            self.res_blocks_specs = [[True] for _ in range(depth+1)]
        else:
            assert_depth_args(res_blocks_specs)
            assert all(isinstance(itm, list) for itm in res_blocks_specs)
            self.res_blocks_specs = res_blocks_specs
        # Same for the decoder:
        if res_blocks_specs_decoder is None:
            # In this case copy setup of the encoder:
            self.res_blocks_specs_decoder = self.res_blocks_specs
        else:
            assert_depth_args(res_blocks_specs_decoder)
            assert all(isinstance(itm, list) for itm in res_blocks_specs_decoder)
            self.res_blocks_specs_decoder = res_blocks_specs_decoder

        # Parse decoder crops:
        self.decoder_crops = decoder_crops if decoder_crops is not None else {}
        assert len(self.decoder_crops) <= depth, "For the moment maximum one crop is supported"

        # Build the skeleton:
        super(MultiInputMultiOutputUNet, self).__init__(depth)

        # Parse output_branches_specs:
        assert isinstance(output_branches_specs, (dict, list))
        nb_branches = len(output_branches_specs)
        assert nb_branches > 0, "At least one output branch should be defined"
        # Create a list from the given dictionary:
        if isinstance(output_branches_specs, dict):
            # Apply global specs to all branches:
            global_specs = output_branches_specs.pop("global")
            nb_branches = len(output_branches_specs)
            collected_specs = [deepcopy(global_specs) for _ in range(nb_branches)]
            for i in range(nb_branches):
                idx = i
                if idx not in output_branches_specs:
                    idx = str(idx)
                    assert idx in output_branches_specs, "Not all the {} specs for the output branches were " \
                                                              "passed".format(nb_branches)
                collected_specs[i].update(output_branches_specs[idx])
            output_branches_specs = collected_specs

        # Build output branches:
        self.output_branches_indices = branch_idxs = {}
        output_branches_collected = []
        for i, branch_specs in enumerate(output_branches_specs):
            assert "out_channels" in branch_specs, "Number of output channels missing for branch {}".format(i)
            assert "depth" in branch_specs, "Depth missing for branch {}".format(i)
            depth = branch_specs["depth"]
            assert isinstance(depth, int)
            # Keep track of ordering of the branches (multiple branches can be passed at the same depth):
            if depth in branch_idxs:
                branch_idxs[depth].append(i)
            else:
                branch_idxs[depth] = [i]
            output_branches_collected.append(self.construct_output_branch(**branch_specs))
        self.output_branches = nn.ModuleList(output_branches_collected)
        print(self.output_branches_indices)

        self.autopad_feature_maps = AutoPad() if number_multiscale_inputs > 1 else None

        self.properly_init_normalizations()

    def forward(self, *inputs):
        nb_inputs = len(inputs)
        assert nb_inputs == self.number_multiscale_inputs, "The number of inputs does not match the one expected " \
                                                           "by the model"

        encoded_states = []
        current = inputs[0]
        for encode, downsample, depth in zip(self.encoder_modules, self.downsampling_modules,
                                      range(self.depth)):
            if depth > 0 and depth < self.number_multiscale_inputs:
                # Pad the features and concatenate the next input:
                current_lvl_padded = self.autopad_feature_maps(current, inputs[depth].shape)
                current = torch.cat((current_lvl_padded, inputs[depth]), dim=1)
                current = encode(current)
            else:
                current = encode(current)
            encoded_states.append(current)
            current = downsample(current)
        current = self.base_module(current)

        outputs = [None for _ in self.output_branches]
        for skip_connection, upsample, merge, decode, depth in reversed(list(zip(
                encoded_states, self.upsampling_modules, self.merge_modules,
                self.decoder_modules, range(len(self.decoder_modules))))):
            current = upsample(current)
            current = merge(current, skip_connection)
            current = decode(current)

            if depth in self.output_branches_indices:
                for branch_idx in self.output_branches_indices[depth]:
                    outputs[branch_idx] = self.output_branches[branch_idx](current)

        if self.return_input:
            outputs = outputs + list(inputs)

        return outputs

    def construct_output_branch(self,
                                depth,
                                out_channels,
                                activation="Sigmoid",
                                normalization=None,
                                **extra_conv_kwargs):
        out_branch = ConvNormActivation(self.decoder_fmaps[depth],
                                         out_channels=out_channels,
                                         kernel_size=1,
                                         dim=3,
                                         activation=activation,
                                         normalization=normalization,
                                         **extra_conv_kwargs)
        crop = self.decoder_crops.get(depth, None)
        out_branch = nn.Sequential(out_branch, Crop(crop)) if crop is not None else out_branch
        return out_branch

    def construct_encoder_module(self, depth):
        if depth == 0:
            f_in = self.in_channels
        elif depth < self.number_multiscale_inputs:
            f_in = self.encoder_fmaps[depth - 1] + self.in_channels
        else:
            f_in = self.encoder_fmaps[depth - 1]
        f_out = self.encoder_fmaps[depth]

        # Build blocks:
        blocks_spec = deepcopy(self.res_blocks_specs[depth])

        if depth == 0:
            first_conv = ConvNormActivation(f_in, f_out, kernel_size=(1, 5, 5),
                                           dim=3,
                                           activation="ReLU",
                                           nb_norm_groups=16,
                                           normalization="GroupNorm")
            # Here the block has a different number of inpiut channels:
            res_block = self.concatenate_res_blocks(f_out, f_out, blocks_spec)
            res_block = nn.Sequential(first_conv, res_block)
        else:
            res_block = self.concatenate_res_blocks(f_in, f_out, blocks_spec)

        return res_block

    def construct_decoder_module(self, depth):
        f_in = self.decoder_fmaps[depth]
        f_out = self.decoder_fmaps[depth]

        # Build blocks:
        blocks_spec = deepcopy(self.res_blocks_specs_decoder[depth])
        res_block = self.concatenate_res_blocks(f_in, f_out, blocks_spec)
        if depth == 0:
            last_conv = ConvNormActivation(f_out, f_out, kernel_size=(1, 5, 5),
                       dim=3,
                       activation="ReLU",
                       nb_norm_groups=16,
                       normalization="GroupNorm")
            res_block = nn.Sequential(res_block, last_conv)
        return res_block

    def construct_base_module(self):
        f_in = self.encoder_fmaps[self.depth - 1]
        f_out = self.encoder_fmaps[self.depth]
        blocks_spec = deepcopy(self.res_blocks_specs[self.depth])
        return self.concatenate_res_blocks(f_in, f_out, blocks_spec)


    def construct_upsampling_module(self, depth):
        # First we need to reduce the numer of channels:
        conv = ConvNormActivation(self.decoder_fmaps[depth+1], self.decoder_fmaps[depth], kernel_size=(1, 1, 1),
                           dim=3,
                           activation="ReLU",
                           nb_norm_groups=16,
                           normalization="GroupNorm")

        scale_factor = self.scale_factors[depth]
        if scale_factor[0] == 1:
            assert scale_factor[1] == scale_factor[2]

        sampler = UpsampleAndCrop(scale_factor=scale_factor, mode=self.upsampling_mode,
                                  crop_slice=self.decoder_crops.get(depth+1, None))

        return nn.Sequential(conv, sampler)

    def construct_downsampling_module(self, depth):
        scale_factor = self.scale_factors[depth]
        sampler = ConvNormActivation(self.encoder_fmaps[depth], self.encoder_fmaps[depth],
                           kernel_size=scale_factor,
                           dim=3,
                           stride=scale_factor,
                           valid_conv=True,
                           activation="ReLU",
                           nb_norm_groups=16,
                           normalization="GroupNorm")
        return sampler


    def construct_merge_module(self, depth):
        return MergeSkipConnAndAutoCrop(self.decoder_fmaps[depth], self.encoder_fmaps[depth])

    def concatenate_res_blocks(self, f_in, f_out, blocks_spec):
        """
        Concatenate multiple residual blocks according to the config file
        """
        # FIXME: generalize
        assert f_out % 16 == 0, "Not divisible by group norm!"

        blocks_list = []
        for is_3D in blocks_spec:
            assert isinstance(is_3D, bool)
            if is_3D:
                blocks_list.append(SamePadResBlock(f_in, f_inner=f_out,
                                                   pre_kernel_size=(3,3,3),
                                                   kernel_size=(3, 3, 3),
                                                   activation="ReLU",
                                                   normalization="GroupNorm",
                                                   nb_norm_groups=16,
                                                   ))
            else:
                blocks_list.append(SamePadResBlock(f_in, f_inner=f_out,
                                                   pre_kernel_size=(1, 3, 3),
                                                   kernel_size=(1, 3, 3),
                                                   activation="ReLU",
                                                   normalization="GroupNorm",
                                                   nb_norm_groups=16,
                                                   ))
            f_in = f_out

        return nn.Sequential(*blocks_list)

    def properly_init_normalizations(self):
        """
        This was sometimes mentioned online as a trick to avoid normalization init problems
        """
        # TODO: check if it is making any difference
        for m in self.modules():
            if isinstance(m, (nn.BatchNorm3d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    @property
    def dim(self):
        return 3


class MergeSkipConnAndAutoCrop(nn.Module):
    """
    Used in the UNet decoder to merge skip connections from feature maps at lower scales
    """
    def __init__(self, nb_prev_fmaps, nb_fmaps_skip_conn):
        super(MergeSkipConnAndAutoCrop, self).__init__()
        if nb_prev_fmaps == nb_fmaps_skip_conn:
            self.conv = Identity()
        else:
            self.conv = ConvNormActivation(nb_fmaps_skip_conn, nb_prev_fmaps, kernel_size=(1, 1, 1),
                                           dim=3,
                                           activation="ReLU",
                                           normalization="GroupNorm",
                                           nb_norm_groups=16)

    def forward(self, tensor, skip_connection):
        if tensor.shape[2:] != skip_connection.shape[2:]:
            target_shape = tensor.shape[2:]
            orig_shape = skip_connection.shape[2:]
            diff = [orig-trg for orig, trg in zip(orig_shape, target_shape)]
            crop_backbone = True
            if not all([d>=0 for d in diff]):
                crop_backbone = False
                orig_shape, target_shape = target_shape, orig_shape
                diff = [orig - trg for orig, trg in zip(orig_shape, target_shape)]
            left_crops = [int(d/2) for d in diff]
            right_crops = [shp-int(d/2) if d%2==0 else shp-(int(d/2)+1)  for d, shp in zip(diff, orig_shape)]
            crop_slice = (slice(None), slice(None)) + tuple(slice(lft,rgt) for rgt,lft in zip(right_crops, left_crops))
            if crop_backbone:
                skip_connection = skip_connection[crop_slice]
            else:
                tensor = tensor[crop_slice]

        return self.conv(skip_connection) + tensor


class AutoPad(nn.Module):
    """
    Used to auto-pad the multiple UNet inputs passed at different resolutions
    """
    def __init__(self):
        super(AutoPad, self).__init__()

    def forward(self, to_be_padded, out_shape):
        in_shape = to_be_padded.shape[2:]
        out_shape = out_shape[2:]
        if in_shape != out_shape:
            diff = [trg-orig for orig, trg in zip(in_shape, out_shape)]
            assert all([d>=0 for d in diff]), "Output shape should be bigger"
            assert all([d % 2 == 0 for d in diff]), "Odd difference in shape!"
            # F.pad expects the last dim first:
            diff.reverse()
            pad = []
            for d in diff:
                pad += [int(d/2), int(d/2)]
            to_be_padded = torch.nn.functional.pad(to_be_padded, tuple(pad), mode='constant', value=0)
        return to_be_padded