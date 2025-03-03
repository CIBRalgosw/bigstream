import sys
import numpy as np
import SimpleITK as sitk
import bigstream.utility as ut
from bigstream.configure_irm import configure_irm
from bigstream.transform import apply_transform, compose_transform_list
from bigstream.metrics import patch_mutual_information
from bigstream import features
import cv2


def apply_alignment_spacing(
    fix,
    mov,
    fix_mask,
    mov_mask,
    fix_spacing,
    mov_spacing,
    alignment_spacing,
):
    """
    Skip sample all images to as close to alignment_spacing as possible
    Determine new voxel spacings

    Parameters
    ----------
    fix : nd-array
        The fixed image

    mov : nd-array
        The moving image

    fix_mask : nd-array
        The fixed image mask (can be None)
        Can have a different shape than fix, but assumed to have the same
        domain or field of view

    mov_mask : nd-array
        The moving image mask (can be None)
        Can have a different shape than mov, but assumed to have the same
        domain or field of view

    fix_spacing : 1d-array
        The fixed image voxel spacing

    mov_spacing : 1d-array
        The moving image voxel spacing

    Returns
    -------
    Returns 8 values in a tuple

    1. skip sampled fixed image
    2. skip sampled moving image
    3. skip sampled fix_mask (or None)
    4. skip sampled mov_mask (or None)
    5. spacing of skip sampled fixed image
    6. spacing of skip sampled moving image
    7. spacing of skip sampled fixed mask (or None)
    8. spacing of skip sampled moving mask (or None)
    """

    # ensure spacings are floating point
    fix_spacing = fix_spacing.astype(np.float64)
    mov_spacing = mov_spacing.astype(np.float64)

    # get mask spacings
    fix_mask_spacing = None
    if fix_mask is not None:
        fix_mask_spacing = ut.relative_spacing(fix_mask, fix, fix_spacing)
    mov_mask_spacing = None
    if mov_mask is not None:
        mov_mask_spacing = ut.relative_spacing(mov_mask, mov, mov_spacing)

    # skip sample
    if alignment_spacing:
        fix, fix_spacing = ut.skip_sample(fix, fix_spacing, alignment_spacing)
        mov, mov_spacing = ut.skip_sample(mov, mov_spacing, alignment_spacing)
        if fix_mask is not None:
            fix_mask, fix_mask_spacing = ut.skip_sample(
                fix_mask, fix_mask_spacing, alignment_spacing,
            )
        if mov_mask is not None:
            mov_mask, mov_mask_spacing = ut.skip_sample(
                mov_mask, mov_mask_spacing, alignment_spacing,
            )

    return (fix, mov, fix_mask, mov_mask,
            fix_spacing, mov_spacing, fix_mask_spacing, mov_mask_spacing,)


def images_to_sitk(
    fix,
    mov,
    fix_mask,
    mov_mask,
    fix_spacing,
    mov_spacing,
    fix_mask_spacing,
    mov_mask_spacing,
    fix_origin,
    mov_origin,
):
    """
    Convert all image inputs to SimpleITK image objects

    Parameters
    ----------
    fix : nd-array
        The fixed image

    mov : nd-array
        The moving image

    fix_mask : nd-array
        The fixed image mask (can be None)

    mov_mask : nd-array
        The moving image mask (can be None)

    fix_spacing : 1d-array
        The voxel spacing of the fixed image

    mov_spacing : 1d-array
        The voxel spacing of the moving image

    fix_mask_spacing : 1d-array
        The voxel spacing of the fixed image mask (can be None)
        fix and fix_mask are assumed to have the same domain,
        but this assumption can be slightly broken after skip_sampling

    mov_mask_spacing : 1d-array
        The voxel spacing of the moving image mask (can be None)
        mov and mov_mask are assumed to have the same domain,
        but this assumption can be slightly broken after skip_sampling

    Returns
    -------
    Returns 4 values in a tuple

    1. fix image as sitk.Image object
    2. mov image as sitk.Image object
    3. fix_mask as sitk.Image object (or None)
    4. mov_mask as sitk.Image object (or None)
    """

    fix = sitk.Cast(ut.numpy_to_sitk(
        fix, fix_spacing, origin=fix_origin), sitk.sitkFloat32)
    mov = sitk.Cast(ut.numpy_to_sitk(
        mov, mov_spacing, origin=mov_origin), sitk.sitkFloat32)
    if fix_mask is not None:
        fix_mask = ut.numpy_to_sitk(
            fix_mask, fix_mask_spacing, origin=fix_origin)
    if mov_mask is not None:
        mov_mask = ut.numpy_to_sitk(
            mov_mask, mov_mask_spacing, origin=mov_origin)
    return fix, mov, fix_mask, mov_mask


def format_static_transform_data(
    transforms,
    fix,
    fix_spacing,
    fix_origin,
):
    """
    Set transform_spacings and transform_origins explicitly

    Parameters
    ----------
    transforms : list of nd-arrays
        The list of static transforms

    fix : nd-array
        The fixed image

    fix_spacing : 1d-array
        The voxel spacing of the fixed image

    fix_origin : 1d-array
        The origin of the fixed image (can be None)

    Returns
    -------
    Returns 2 values in a tuple

    1. The tuple of transform spacings
    2. The tuple of transform origins
    """

    spacings = []
    for transform in transforms:
        spacing = fix_spacing
        if len(transform.shape) not in [1, 2]:
            spacing = ut.relative_spacing(transform, fix, fix_spacing)
        spacings.append(spacing)
    spacings = tuple(spacings)
    origins = (fix_origin,)*len(transforms)
    return (spacings, origins)


def feature_point_ransac_affine_align(
    fix, mov,
    fix_spacing,
    mov_spacing,
    blob_sizes,
    alignment_spacing=None,
    num_sigma_max=15,
    cc_radius=12,
    nspots=5000,
    match_threshold=0.7,
    max_spot_match_distance=None,
    align_threshold=2.0,
    diagonal_constraint=0.25,
    fix_spot_detection_kwargs={},
    mov_spot_detection_kwargs={},
    fix_spots=None,
    mov_spots=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    static_transform_list=[],
    default=None,
    **kwargs,
):
    """
    Currently this function only works on 3D images.

    Compute an affine alignment from feature points and ransac.
    A blob detector finds feature points in fix and mov. Correspondence
    between the fix and mov point sets is estimated using neighborhood
    correlation. A ransac filter determines the affine transform that brings
    the largest number of corresponding points to the same locations.

    At least 100 spots must be found in the fixed image and 100 spots
    in the moving image, otherwise default is returned. At least 50
    correspondence pairs must be found, otherwise default is returned.
    These constraints are required for reasonable performance from the
    ransac affine alignment algorithm.

    If insufficient points are found modify fix_spot_detection_kwargs
    and/or mov_spot_detection_kwargs. See bigstream.features.

    If insufficient point matches are found, modify match_threshold.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    blob_sizes : list of two floats
        The [minimum, maximum] size of feature point objects in voxel units

    num_sigma_max : scalar int (default: 15)
        The maximum number of laplacians to use in the feature point LoG detector

    cc_radius : scalar int (default: 12)
        The halfwidth of neighborhoods around feature points used to determine
        correlation and correspondence

    nspots : scalar int (default: 5000)
        The maximum number of feature point spots to use in each image
        If more spots are found the brightest ones are used.

    match_threshold : scalar float in range [0, 1] (default: 0.7)
        The minimum correlation two feature point neighborhoods must have to
        consider them corresponding points

    max_spot_match_distance : scalar float (default: None)
        The maximum distance a fix and mov spot can be before alignment
        to still be considered matching spots; in microns. This helps
        prevent false positive correspondences.

    align_threshold : scalar float (default: 2.0)
        The maximum distance two points can be to be considered aligned
        by the affine transform; in microns.

    diagonal_constraint : scalar float (default: 0.25)
        Diagonal entries of the affine matrix cannot be lower than
        1 - diagonal_contraint or higher than 1 + diagonal_contraint. 
        If this condition is violated the default transform is returned.
        This helps prevent bad alignments.

    fix_spot_detection_kwargs : dict (default {})
        Arguments passed to bigstream.features.blob_detection for fixed image

    mov_spot_detection_kwargs : dict (default {})
        Arguments passed to bigstream.features.blob_detection for moving image

    fix_spots : nd-array Nx3 (default: None)
        Skip the spot detection for the fixed image and provide your own spot coordinate

    mov_spots : nd-array Nx3 (default: None)
        Skip the spot detection for the moving image and provide your own spot coordinate

    fix_mask : binary nd-array (default: None)
        Spots from fixed image can only be found in the foreground of this mask

    mov_mask : binary nd-array (default: None)
        Spots from moving image can only be found in the foreground of this mask

    fix_origin : 1d array (default: all zeros)
        The origin of the fixed image in physical units

    mov_origin : 1d array (default: all zeros)
        The origin of the moving image in physical units

    static_transform_list : list of numpy arrays (default: [])
        Transforms applied to moving image before applying query transform
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    default : 2d array 4x4 (default: identity)
        A default transform to return if the method fails to find a valid one

    **kwargs : any additional keyword arguments
        Passed to cv2.estimateAffine3D

    Returns
    -------
    affine_matrix : 2d array 4x4
        An affine matrix matching the moving image to the fixed image
    """

    # establish default
    if default is None: default = np.eye(fix.ndim + 1)

    # apply static transforms
    if static_transform_list:
        mov = apply_transform(
            fix, mov, fix_spacing, mov_spacing,
            transform_list=static_transform_list,
            fix_origin=fix_origin,
            mov_origin=mov_origin,
        )
        if mov_mask is not None:
            mov_mask = apply_transform(
                fix.astype(mov_mask.dtype), mov_mask,
                fix_spacing, mov_spacing,
                transform_list=static_transform_list,
                fix_origin=fix_origin, 
                mov_origin=mov_origin,
                interpolator='0',
            )
        mov_spacing = fix_spacing

    # skip sample and determine mask spacings
    X = apply_alignment_spacing(
        fix, mov,
        fix_mask, mov_mask,
        fix_spacing, mov_spacing,
        alignment_spacing,
    )
    fix = X[0]
    mov = X[1]
    fix_mask = X[2]
    mov_mask = X[3]
    fix_spacing = X[4]
    mov_spacing = X[5]
    fix_mask_spacing = X[6]
    mov_mask_spacing = X[7]

    # get fix spots
    num_sigma = int(min(blob_sizes[1] - blob_sizes[0], num_sigma_max))
    print('computing fixed spots', flush=True)
    if fix_spots is None:
        fix_kwargs = {
            'num_sigma':num_sigma,
            'exclude_border':cc_radius,
            'mask':fix_mask,
        }
        fix_kwargs = {**fix_kwargs, **fix_spot_detection_kwargs}
        fix_spots = features.blob_detection(
            fix, blob_sizes[0], blob_sizes[1],
            **fix_kwargs,
        )
    print(f'found {len(fix_spots)} fixed spots')
    if len(fix_spots) < 100:
        print('insufficient fixed spots found, returning default', flush=True)
        return default

    # get mov spots
    print('computing moving spots', flush=True)
    if mov_spots is None:
        mov_kwargs = {
            'num_sigma':num_sigma,
            'exclude_border':cc_radius,
            'mask':mov_mask,
        }
        mov_kwargs = {**mov_kwargs, **mov_spot_detection_kwargs}
        mov_spots = features.blob_detection(
            mov, blob_sizes[0], blob_sizes[1],
            **mov_kwargs,
        )
    print(f'found {len(mov_spots)} moving spots')
    if len(mov_spots) < 100:
        print('insufficient moving spots found, returning default', flush=True)
        return default

    # sort
    print('sorting spots', flush=True)
    sort_idx = np.argsort(fix_spots[:, 3])[::-1]
    fix_spots = fix_spots[sort_idx, :3][:nspots]
    sort_idx = np.argsort(mov_spots[:, 3])[::-1]
    mov_spots = mov_spots[sort_idx, :3][:nspots]

    # get contexts
    print('extracting contexts', flush=True)
    fix_spot_contexts = features.get_contexts(fix, fix_spots, cc_radius)
    mov_spot_contexts = features.get_contexts(mov, mov_spots, cc_radius)

    # get pairwise correlations
    print('computing pairwise correlations', flush=True)
    correlations = features.pairwise_correlation(
        fix_spot_contexts, mov_spot_contexts,
    )

    # convert to physical units
    fix_spots = fix_spots * fix_spacing
    mov_spots = mov_spots * mov_spacing

    # get matching points
    fix_spots, mov_spots = features.match_points(
        fix_spots, mov_spots,
        correlations, match_threshold,
        max_distance=max_spot_match_distance,
    )
    print(f'found {len(fix_spots)} matched spot pairs')
    if len(fix_spots) < 50 or len(mov_spots) < 50:
        print('insufficient spot matches found, returning default', flush=True)
        return default

    # align
    # TODO: this is a hard 3D constraint, check opencv for a 2D version
    print('aligning', flush=True)
    r, Aff, inline = cv2.estimateAffine3D(
        fix_spots, mov_spots,
        ransacThreshold=align_threshold,
        confidence=0.999,
        **kwargs,
    )

    # ensure affine is sensible
    if np.any( np.abs(np.diag(Aff) - 1) > diagonal_constraint ):
        print("Degenerate affine produced, returning default", flush=True)
        return default

    # augment matrix and return
    affine = np.eye(fix.ndim + 1)
    affine[:fix.ndim, :] = Aff
    return affine


def random_affine_search(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    random_iterations,
    nreturn=1,
    max_translation=None,
    max_rotation=None,
    max_scale=None,
    max_shear=None,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    static_transform_list=[],
    use_patch_mutual_information=False,
    print_running_improvements=False,
    **kwargs,
):
    """
    Apply random affine matrices within given bounds to moving image.
    This function is intended to find good initialization for a full affine
    alignment obtained by calling `affine_align`

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    random_iterations : int
        The number of random affine matrices to sample

    nreturn : int (default: 1)
        The number of affine matrices to return. The best scoring results
        are returned.

    max_translation : float or tuple of float
        The maximum amplitude translation allowed in random sampling.
        Specified in physical units (e.g. um or mm)
        Can be specified per axis.

    max_rotation : float or tuple of float
        The maximum amplitude rotation allowed in random sampling.
        Specified in radians
        Can be specified per axis.

    max_scale : float or tuple of float
        The maximum amplitude scaling allowed in random sampling.
        Can be specified per axis.

    max_shear : float or tuple of float
        The maximum amplitude shearing allowed in random sampling.
        Can be specified per axis.

    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image
        Assumed to have the same domain as the moving image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    static_transform_list : list of numpy arrays (default: [])
        Transforms applied to moving image before applying query transform
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    use_patch_mutual_information : bool (default: False)
        Uses a custom metric function in bigstream.metrics

    print_running_improvements : bool (default: False)
        If True, whenever a better transform is found print the
        iteration, score, and parameters

    **kwargs : any additional arguments
        Passed to `configure_irm` This is how you customize the metric.
        If `use_path_mutual_information` is True this is passed to
        the `patch_mutual_information` function instead.

    Returns
    -------
    best transforms : sorted list of 4x4 numpy.ndarrays (affine matrices)
        best nreturn results, first element of list is the best result
    """

    # function to help generalize parameter limits to 3d
    def expand_param_to_3d(param, null_value):
        if isinstance(param, (int, float)):
            param = (param,) * 2
        if isinstance(param, tuple):
            param += (null_value,)
        return param

    # TODO: consider moving to native 2D
    # generalize 2d inputs to 3d
    if fix.ndim == 2:
        fix = fix.reshape(fix.shape + (1,))
        mov = mov.reshape(mov.shape + (1,))
        fix_spacing = tuple(fix_spacing) + (1.,)
        mov_spacing = tuple(mov_spacing) + (1.,)
        max_translation = expand_param_to_3d(max_translation, 0)
        max_rotation = expand_param_to_3d(max_rotation, 0)
        max_scale = expand_param_to_3d(max_scale, 1)
        max_shear = expand_param_to_3d(max_shear, 0)
        if fix_mask is not None: fix_mask = fix_mask.reshape(fix_mask.shape + (1,))
        if mov_mask is not None: mov_mask = mov_mask.reshape(mov_mask.shape + (1,))
        if fix_origin is not None: fix_origin = tuple(fix_origin) + (0.,)
        if mov_origin is not None: mov_origin = tuple(mov_origin) + (0.,)

    # generate random parameters, first row is always identity
    params = np.zeros((random_iterations+1, 12))
    params[:, 6:9] = 1  # default for scale params
    F = lambda mx: 2 * (mx * np.random.rand(random_iterations, 3)) - mx
    if max_translation: params[1:, 0:3] = F(max_translation)
    if max_rotation: params[1:, 3:6] = F(max_rotation)
    if max_scale: params[1:, 6:9] = np.e**F(np.log(max_scale))
    if max_shear: params[1:, 9:] = F(max_shear)
    center = np.array(fix.shape) / 2 * fix_spacing  # center of rotation

    # format static transform data explicitly
    a, b = format_static_transform_data(
        static_transform_list, fix, fix_spacing, fix_origin,
    )
    static_transform_spacing = a
    static_transform_origin = b

    # skip sample and determine mask spacings
    X = apply_alignment_spacing(
        fix, mov,
        fix_mask, mov_mask,
        fix_spacing, mov_spacing,
        alignment_spacing,
    )
    fix = X[0]
    mov = X[1]
    fix_mask = X[2]
    mov_mask = X[3]
    fix_spacing = X[4]
    mov_spacing = X[5]
    fix_mask_spacing = X[6]
    mov_mask_spacing = X[7]

    # a useful value later, storing prevents redundant function calls
    WORST_POSSIBLE_SCORE = np.finfo(np.float64).max

    # define metric evaluation
    if use_patch_mutual_information:
        # wrap patch_mi metric
        def score_affine(affine):
            # apply transform
            transform_list = static_transform_list + [affine,]
            aligned = apply_transform(
                fix, mov, fix_spacing, mov_spacing,
                transform_list=transform_list,
                fix_origin=fix_origin,
                mov_origin=mov_origin,
                transform_spacing=static_transform_spacing,
                transform_origin=static_transform_origin,
            )
            mov_mask_aligned = None
            if mov_mask is not None:
                mov_mask_aligned = apply_transform(
                    fix_mask, mov_mask, fix_mask_spacing, mov_mask_spacing,
                    transform_list=transform_list,
                    fix_origin=fix_origin,
                    mov_origin=mov_origin,
                    transform_spacing=static_transform_spacing,
                    transform_origin=static_transform_origin,
                    interpolator='0',
                )
            # evaluate metric
            # TODO: this function needs to be updated for different
            #       mask and image sizes
            return patch_mutual_information(
                fix, aligned, fix_spacing,
                fix_mask=fix_mask,
                mov_mask=mov_mask_aligned,
                return_metric_image=False,
                **kwargs,
            )

    # use an irm metric
    else:
        # construct irm, set images, masks, transforms
        kwargs['optimizer'] = 'LBFGS2'    # optimizer is not used, just a dummy value
        kwargs['optimizer_args'] = {}
        irm = configure_irm(**kwargs)
        fix, mov, fix_mask, mov_mask = images_to_sitk(
            fix, mov, fix_mask, mov_mask,
            fix_spacing, mov_spacing,
            fix_mask_spacing, mov_mask_spacing,
            fix_origin, mov_origin,
        )
        if fix_mask is not None: irm.SetMetricFixedMask(fix_mask)
        if mov_mask is not None: irm.SetMetricMovingMask(mov_mask)
        if static_transform_list:
            T = ut.transform_list_to_composite_transform(
                static_transform_list,
                static_transform_spacing,
                static_transform_origin,
            )
            irm.SetMovingInitialTransform(T)

        # wrap irm metric
        def score_affine(affine):
            irm.SetInitialTransform(ut.matrix_to_affine_transform(affine))
            try:
                return irm.MetricEvaluate(fix, mov)
            except Exception as e:
                return WORST_POSSIBLE_SCORE

    # score all random affines
    current_best_score = WORST_POSSIBLE_SCORE
    scores = np.empty(random_iterations + 1, dtype=np.float64)
    for iii, ppp in enumerate(params):
        scores[iii] = score_affine(ut.physical_parameters_to_affine_matrix_3d(ppp, center))
        if print_running_improvements and scores[iii] < current_best_score:
                current_best_score = scores[iii]
                print(iii, ': ', current_best_score, '\n', ppp)
    sys.stdout.flush()

    # return top results
    partition_indx = np.argpartition(scores, nreturn)[:nreturn]
    params, scores = params[partition_indx], scores[partition_indx]
    return [ut.physical_parameters_to_affine_matrix_3d(p, center) for p in params[np.argsort(scores)]]


def affine_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    rigid=False,
    initial_condition=None,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    static_transform_list=[],
    default=None,
    **kwargs,
):
    """
    Affine or rigid alignment of a fixed/moving image pair.
    Lots of flexibility in speed/accuracy trade off.
    Highly configurable and useful in many contexts.

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.
        Length must equal `mov.ndim`

    rigid : bool (default: False)
        Restrict the alignment to rigid motion only

    initial_condition : str or 4x4 ndarray (default: None)
        How to begin the optimization. Only one string value is allowed:
        "CENTER" in which case the alignment is initialized by a center
        of mass alignment. If a 4x4 ndarray is given the optimization
        is initialized with that transform.

    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image
        Assumed to have the same domain as the moving image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    static_transform_list : list of numpy arrays (default: [])
        Transforms applied to moving image before applying query transform
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    default : 4x4 array (default: identity matrix)
        If the optimization fails, print error message but return this value

    **kwargs : any additional arguments
        Passed to `configure_irm`
        This is where you would set things like:
        metric, iterations, shrink_factors, and smooth_sigmas

    Returns
    -------
    transform : 4x4 array
        The affine or rigid transform matrix matching moving to fixed
    """

    # determine the correct default
    if default is None: default = np.eye(fix.ndim + 1)
    initial_transform_given = isinstance(initial_condition, np.ndarray)
    if initial_transform_given and np.all(default == np.eye(fix.ndim + 1)):
        default = initial_condition

    # format static transform data explicitly
    a, b = format_static_transform_data(
        static_transform_list, fix, fix_spacing, fix_origin,
    )
    static_transform_spacing = a
    static_transform_origin = b

    # skip sample and convert inputs to sitk images
    X = apply_alignment_spacing(
        fix, mov,
        fix_mask, mov_mask,
        fix_spacing, mov_spacing,
        alignment_spacing,
    )
    fix, mov, fix_mask, mov_mask = images_to_sitk(
        *X, fix_origin, mov_origin,
    )
    fix_spacing = X[4]
    mov_spacing = X[5]
    fix_mask_spacing = X[6]
    mov_mask_spacing = X[7]

    # set up registration object
    irm = configure_irm(**kwargs)
    # set initial static transforms
    if static_transform_list:
        T = ut.transform_list_to_composite_transform(
            static_transform_list,
            static_transform_spacing,
            static_transform_origin,
        )
        irm.SetMovingInitialTransform(T)

    # distinguish between 2D and 3D for rigid transforms
    ndims = fix.GetDimension()
    rigid_transform_constructor = sitk.Euler2DTransform if ndims == 2 else sitk.Euler3DTransform

    # set transform to optimize
    if isinstance(initial_condition, str) and initial_condition == "CENTER":
        a, b = fix, mov
        if fix_mask is not None and mov_mask is not None:
            a, b = fix_mask, mov_mask
        x = sitk.CenteredTransformInitializer(a, b, rigid_transform_constructor())
        x = rigid_transform_constructor(x).GetTranslation()[::-1]
        initial_condition = np.eye(ndims+1)
        initial_condition[:ndims, -1] = x
        initial_transform_given = True
    if rigid and not initial_transform_given:
        transform = rigid_transform_constructor()
    elif rigid and initial_transform_given:
        transform = ut.matrix_to_euler_transform(initial_condition)
    elif not rigid and not initial_transform_given:
        transform = sitk.AffineTransform(fix.GetDimension())
    elif not rigid and initial_transform_given:
        transform = ut.matrix_to_affine_transform(initial_condition)
    irm.SetInitialTransform(transform, inPlace=True)
    # set masks
    if fix_mask is not None: irm.SetMetricFixedMask(fix_mask)
    if mov_mask is not None: irm.SetMetricMovingMask(mov_mask)

    # execute alignment, for any exceptions return default
    try:
        initial_metric_value = irm.MetricEvaluate(fix, mov)
        irm.Execute(fix, mov)
        final_metric_value = irm.MetricEvaluate(fix, mov)
    except Exception as e:
        print("Registration failed due to ITK exception:\n", e)
        print("Returning default", flush=True)
        return default

    # if registration improved metric return result
    # otherwise return default
    if final_metric_value < initial_metric_value:
        print("Registration succeeded", flush=True)
        return ut.affine_transform_to_matrix(transform)
    else:
        print("Optimization failed to improve metric")
        print(f"METRIC VALUES initial: {initial_metric_value} final: {final_metric_value}")
        print("Returning default", flush=True)
        return default


def deformable_align(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    control_point_spacing,
    control_point_levels,
    alignment_spacing=None,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    static_transform_list=[],
    default=None,
    **kwargs,
):
    """
    Register moving to fixed image with a bspline parameterized deformation field

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.

    control_point_spacing : float
        The spacing in physical units (e.g. mm or um) between control
        points that parameterize the deformation. Smaller means
        more precise alignment, but also longer compute time. Larger
        means shorter compute time and smoother transform, but less
        precise.

    control_point_levels : list of type int
        The optimization scales for control point spacing. E.g. if
        `control_point_spacing` is 100.0 and `control_point_levels`
        is [4, 2, 1] then method will optimize at 400.0 units control
        points spacing, then optimize again at 200.0 units, then again
        at the requested 100.0 units control point spacing.
    
    alignment_spacing : float (default: None)
        Fixed and moving images are skip sampled to a voxel spacing
        as close as possible to this value. Intended for very fast
        simple alignments (e.g. low amplitude motion correction)

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image
        Assumed to have the same domain as the moving image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    static_transform_list : list of numpy arrays (default: [])
        Transforms applied to moving image before applying query transform
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    default : any object (default: None)
        If optimization fails to improve image matching metric,
        print an error but also return this object. If None
        the parameters and displacement field for an identity
        transform are returned.

    **kwargs : any additional arguments
        Passed to `configure_irm`
        This is where you would set things like:
        metric, iterations, shrink_factors, and smooth_sigmas

    Returns
    -------
    params : 1d array
        The complete set of control point parameters concatenated
        as a 1d array.

    field : ndarray
        The displacement field parameterized by the bspline control
        points
    """

    # store initial fixed image shape
    initial_fix_shape = fix.shape
    initial_fix_spacing = fix_spacing

    # format static transform data explicitly
    a, b = format_static_transform_data(
        static_transform_list, fix, fix_spacing, fix_origin,
    )
    static_transform_spacing = a
    static_transform_origin = b

    # skip sample and convert inputs to sitk images
    X = apply_alignment_spacing(
        fix, mov,
        fix_mask, mov_mask,
        fix_spacing, mov_spacing,
        alignment_spacing,
    )
    fix, mov, fix_mask, mov_mask = images_to_sitk(
        *X, fix_origin, mov_origin,
    )
    fix_spacing = X[4]
    mov_spacing = X[5]
    fix_mask_spacing = X[6]
    mov_mask_spacing = X[7]

    # set up registration object
    irm = configure_irm(**kwargs)

    # initial control point grid
    z = control_point_spacing * control_point_levels[0]
    initial_cp_grid = [max(1, int(x*y/z)) for x, y in zip(fix.GetSize(), fix.GetSpacing())]
    transform = sitk.BSplineTransformInitializer(
        image1=fix, transformDomainMeshSize=initial_cp_grid, order=3,
    )
    irm.SetInitialTransformAsBSpline(
        transform, inPlace=True, scaleFactors=control_point_levels[::-1],
    )

    # set initial static transforms
    if static_transform_list:
        T = ut.transform_list_to_composite_transform(
            static_transform_list,
            static_transform_spacing,
            static_transform_origin,
        )
        irm.SetMovingInitialTransform(T)
    # set masks
    if fix_mask is not None: irm.SetMetricFixedMask(fix_mask)
    if mov_mask is not None: irm.SetMetricMovingMask(mov_mask)

    # now we can set the default
    if not default:
        params = np.concatenate((transform.GetFixedParameters(), transform.GetParameters()))
        field = ut.bspline_to_displacement_field(
            transform, initial_fix_shape,
            spacing=initial_fix_spacing, origin=fix_origin,
            direction=np.eye(fix.GetDimension()),
        )
        default = (params, field)

    # execute alignment, for any exceptions return default
    try:
        initial_metric_value = irm.MetricEvaluate(fix, mov)
        irm.Execute(fix, mov)
        final_metric_value = irm.MetricEvaluate(fix, mov)
    except Exception as e:
        print("Registration failed due to ITK exception:\n", e)
        print("Returning default", flush=True)
        return default

    # if registration improved metric return result
    # otherwise return default
    if final_metric_value < initial_metric_value:
        params = np.concatenate((transform.GetFixedParameters(), transform.GetParameters()))
        field = ut.bspline_to_displacement_field(
            transform, initial_fix_shape,
            spacing=initial_fix_spacing, origin=fix_origin,
            direction=np.eye(fix.GetDimension()),
        )
        print("Registration succeeded", flush=True)
        return params, field
    else:
        print("Optimization failed to improve metric")
        print(f"METRIC VALUES initial: {initial_metric_value} final: {final_metric_value}")
        print("Returning default", flush=True)
        return default


def alignment_pipeline(
    fix,
    mov,
    fix_spacing,
    mov_spacing,
    steps,
    fix_mask=None,
    mov_mask=None,
    fix_origin=None,
    mov_origin=None,
    static_transform_list=[],
    return_format='flatten',
    **kwargs,
):
    """
    Compose random, rigid, affine, and deformable alignments with one function call

    Parameters
    ----------
    fix : ndarray
        the fixed image

    mov : ndarray
        the moving image; `fix.ndim` must equal `mov.ndim`

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image.
        Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image.

    steps : list of tuples in this form [(str, dict), (str, dict), ...]
        For each tuple, the str specifies which alignment to run. The options are:
        'ransac' : run `feature_point_ransac_affine_align`
        'random' : run `random_affine_search`
        'rigid' : run `affine_align` with `rigid=True`
        'affine' : run `affine_align`
        'deform' : run `deformable_align`
        For each tuple, the dict specifies the arguments to that alignment function
        Arguments specified here override any global arguments given through kwargs
        for their specific step only.

    fix_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the fixed image
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    mov_mask : binary ndarray (default: None)
        A mask limiting metric evaluation region of the moving image
        Assumed to have the same domain as the moving image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    fix_origin : 1d array (default: None)
        Origin of the fixed image.
        Length must equal `fix.ndim`

    mov_origin : 1d array (default: None)
        Origin of the moving image.
        Length must equal `mov.ndim`

    static_transform_list : list of numpy arrays (default: [])
        Transforms applied to moving image before applying query transform
        Assumed to have the same domain as the fixed image, though sampling
        can be different. I.e. the origin and span are the same (in phyiscal
        units) but the number of voxels can be different.

    return_format : str (default: 'flatten')
        The way in which transforms are returned to the user. Options are:
        'independent' : one transform per step is returned, no compositions
        'compressed' : adjacent affines and adjacent deforms are composed,
                       but affines are not composed with deforms. For example:
                       ['random', 'affine', 'deform', 'deform', 'affine', 'deform']
                       will return a list of 4 transforms.
        'flatten' : compose all transforms regardless of type into a single transform

    **kwargs : any additional keyword arguments
        Global arguments that apply to all alignment steps
        These are overwritten by specific arguments passed via
        the dictionaries in steps

    Returns
    -------
    transform : ndarray or tuple of ndarray
        Transform(s) aligning moving to fixed image.

        If 'deform' is not in `steps` then this is a single 4x4 matrix - all
        steps ('random', 'rigid', and/or 'affine') are composed.

        If 'deform' is in `steps` then this is a tuple. The first element
        is the composed 4x4 affine matrix, the second is the output of
        deformable align: a tuple with the bspline parameters and the
        vector field with shape equal to fix.shape + (3,)
    """

    # define how to run alignment functions
    a = (fix, mov, fix_spacing, mov_spacing)
    b = {'fix_mask':fix_mask, 'mov_mask':mov_mask,
         'fix_origin':fix_origin, 'mov_origin':mov_origin,}
    align = {'ransac':lambda **c: feature_point_ransac_affine_align(*a, **{**b, **c}),
             'random':lambda **c: random_affine_search(*a, **{**b, **c})[0],
             'rigid': lambda **c: affine_align(*a, **{**b, **c}, rigid=True),
             'affine':lambda **c: affine_align(*a, **{**b, **c}),
             'deform':lambda **c: deformable_align(*a, **{**b, **c})[1],}

    # loop over steps
    new_transforms = []
    for alignment, arguments in steps:
        arguments = {**kwargs, **arguments}
        arguments['static_transform_list'] = static_transform_list + new_transforms
        new_transforms.append(align[alignment](**arguments))

    # return in the requested format
    if return_format == 'independent':
        return new_transforms
    elif return_format == 'compressed':
        shapes = np.array([x.shape for x in new_transforms], dtype=object)
        changes = np.where(shapes[:-1] != shapes[1:])[0] + 1
        changes = [0,] + list(changes) + [len(new_transforms),]
        F = lambda a, b: compose_transform_list(new_transforms[a:b], fix_spacing)
        return [F(a, b) for a, b in zip(changes[:-1], changes[1:])]
    elif return_format == 'flatten':
        return compose_transform_list(new_transforms, fix_spacing)

