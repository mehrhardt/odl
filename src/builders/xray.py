# -*- coding: utf-8 -*-
"""
xray.py -- helper functions for X-ray tomography

Copyright 2014, 2015 Holger Kohr

This file is part of RL.

RL is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

RL is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with RL.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

import numpy as np
from math import pi

from RL.datamodel import ugrid as ug
from RL.datamodel import gfunc as gf
from RL.geometry import curve as crv
from RL.geometry import source as src
from RL.geometry import sample as spl
from RL.geometry import detector as det
from RL.geometry import geometry as geo
from RL.operator import projector as proj
from RL.operator.projector import Projector, BackProjector
from RL.utility.utility import InputValidationError, errfmt


def xray_ct_parallel_3d_projector(geometry, backend='astra_cuda'):

    # FIXME: this construction is probably only temporary. Think properly
    # about how users would construct projectors
    return Projector(xray_ct_parallel_projection_3d, geometry,
                     backend=backend)


def xray_ct_parallel_3d_backprojector(geometry, backend='astra_cuda'):

    # FIXME: this construction is probably only temporary. Think properly
    # about how users would construct projectors
    return Projector(xray_ct_parallel_backprojection_3d, geometry,
                     backend=backend)


def xray_ct_parallel_geom_3d(spl_grid, det_grid, axis, angles=None,
                             rotating_sample=True, **kwargs):
    """
    Create a 3D parallel beam geometry for X-ray CT with a flat detector.

    Parameters
    ----------
    spl_grid: ugrid.Ugrid
        3D grid for the sample domain
    det_grid: ugrid.Ugrid
        2D grid for the detector domain
    axis: int or array-like
        rotation axis; if integer, interpreted as corresponding standard
        unit vecor
    angles: array-like, optional
        specifies the rotation angles
    rotating_sample: boolean, optional
        if True, the sample rotates, otherwise the source-detector system

    Keyword arguments
    -----------------
    init_rotation: matrix-like or float
        initial rotation of the sample; if float, ???

    Returns
    -------
    out: geometry.Geometry
        the new parallel beam geometry
    """

    spl_grid = ug.ugrid(spl_grid)
    det_grid = ug.ugrid(det_grid)
    if not spl_grid.dim == 3:
        raise InputValidationError(spl_grid.dim, 3, 'spl_grid.dim')
    if not det_grid.dim == 2:
        raise InputValidationError(det_grid.dim, 2, 'det_grid.dim')

    if angles is not None:
        angles = np.array(angles)

    init_rotation = kwargs.get('init_rotation', None)

    if rotating_sample:
        # TODO: make axis between source and detector flexible; now: -x axis
        direction = (1., 0., 0.)
        src_loc = (-1., 0., 0.)
        source = src.ParallelRaySource(direction, src_loc)
        sample = spl.RotatingGridSample(spl_grid, axis, init_rotation,
                                        angles=angles, **kwargs)
        det_loc = (1., 0., 0.)
        detector = det.FlatDetectorArray(det_grid, det_loc)
    else:
        src_circle = crv.Circle3D(1., axis, angles=angles, axes_map='tripod')
        source = src.ParallelRaySource((1, 0, 0), src_circle)

        sample = spl.FixedSample(spl_grid)

        det_circle = crv.Circle3D(1., axis, angle_shift=pi, angles=angles,
                                  axes_map='tripod')
        detector = det.FlatDetectorArray(det_grid, det_circle)
    return geo.Geometry(source, sample, detector)


def xray_ct_parallel_projection_3d(geometry, vol_func, backend='astra_cuda'):

    if backend == 'astra':
        proj_func = _xray_ct_par_fp_3d_astra(geometry, vol_func,
                                             use_cuda=False)
    elif backend == 'astra_cuda':
        proj_func = _xray_ct_par_fp_3d_astra(geometry, vol_func,
                                             use_cuda=True)
    else:
        raise NotImplementedError(errfmt('''\
        Only `astra` and `astra_cuda` backends supported'''))

    return proj_func


def _xray_ct_par_fp_3d_astra(geom, vol, use_cuda=True):

    import astra as at

    # FIXME: we assume fixed sample rotating around z axis for now
    # TODO: include shifts (volume and detector)
    # TODO: allow custom detector grid (e.g. only partial projection)

    # Initialize volume geometry and data

    # ASTRA uses a different axis labeling. We need to cycle x->y->z->x
    astra_vol = vol.fvals.swapaxes(0, 1).swapaxes(0, 2)
    astra_vol_geom = at.create_vol_geom(vol.shape)

    # Initialize detector geometry

    # Detector pixel spacing must be scaled with volume (y,z) spacing
    # since ASTRA assumes voxel size 1
    # FIXME: assuming z axis tilt
    det_grid = geom.detector.grid
    astra_pixel_spacing = det_grid.spacing / vol.spacing[1:]

    # FIXME: treat case when no discretization is given

    # FIXME: adjust angles if the scaling is not uniform
    astra_angles = geom.sample.angles

    # ASTRA lables detector axes as 'rows, columns', so we need to swap
    # axes 0 and 1
    astra_proj_geom = at.create_proj_geom('parallel3d',
                                          astra_pixel_spacing[0],
                                          astra_pixel_spacing[1],
                                          det_grid.shape[1],
                                          det_grid.shape[0],
                                          astra_angles)

    # Some wrapping code
    astra_vol_id = at.data3d.create('-vol', astra_vol_geom, astra_vol)
    astra_proj_id = at.data3d.create('-sino', astra_proj_geom)

    # Create the ASTRA algorithm
    if use_cuda:
        astra_algo_conf = at.astra_dict('FP3D_CUDA')
    else:
        # TODO: slice into 2D forward projections
        raise NotImplementedError('No CPU 3D forward projection available.')

    astra_algo_conf['VolumeDataId'] = astra_vol_id
    astra_algo_conf['ProjectionDataId'] = astra_proj_id
    astra_algo_id = at.algorithm.create(astra_algo_conf)

    # Run it and remove afterwards
    at.algorithm.run(astra_algo_id)
    at.algorithm.delete(astra_algo_id)

    # Get the projection data. ASTRA creates an (nrows, ntilts, ncols) array,
    # so we need to cycle to the right to get (nx, ny, ntilts)
    proj_fvals = at.data3d.get(astra_proj_id)
    proj_fvals = proj_fvals.swapaxes(1, 2).swapaxes(0, 1)

    # Create the projection grid function and return it
    proj_spacing = np.ones(3)
    proj_spacing[:-1] = det_grid.spacing
    proj_func = gf.Gfunc(proj_fvals, spacing=proj_spacing)

    return proj_func


def xray_ct_parallel_backprojection_3d(geometry, proj_func,
                                       backend='astra_cuda'):

    if backend == 'astra':
        vol_func = _xray_ct_par_bp_3d_astra(geometry, proj_func,
                                            use_cuda=False)
    elif backend == 'astra_cuda':
        vol_func = _xray_ct_par_bp_3d_astra(geometry, proj_func,
                                            use_cuda=True)
    else:
        raise NotImplementedError(errfmt('''\
        Only `astra` and `astra_cuda` backends supported'''))

    return vol_func


def _xray_ct_par_bp_3d_astra(geom, proj_, use_cuda=True):

    import astra as at

    # FIXME: we assume fixed sample rotating around z axis for now
    # TODO: include shifts (volume and detector)
    # TODO: allow custom volume grid (e.g. partial backprojection)

    # Initialize projection geometry and data

    # Detector pixel spacing must be scaled with volume (y,z) spacing
    # since ASTRA assumes voxel size 1
    # FIXME: assuming z axis tilt
    vol_grid = geom.sample.grid
    astra_pixel_spacing = proj_.spacing[:-1] / vol_grid.spacing[1:]

    # FIXME: treat case when no discretization is given
    astra_angles = geom.sample.angles

    # ASTRA assumes a (nrows, ntilts, ncols) array. We must cycle axes to
    # the left and swap detector axes in the geometry.
    astra_proj = proj_.fvals.swapaxes(0, 2).swapaxes(0, 1)
    astra_proj_geom = at.create_proj_geom('parallel3d',
                                          astra_pixel_spacing[0],
                                          astra_pixel_spacing[1],
                                          proj_.shape[1],
                                          proj_.shape[0],
                                          astra_angles)

    # Initialize volume geometry
    astra_vol_geom = at.create_vol_geom(vol_grid.shape)

    # Some wrapping code
    astra_vol_id = at.data3d.create('-vol', astra_vol_geom)
    astra_proj_id = at.data3d.create('-sino', astra_proj_geom, astra_proj)

    # Create the ASTRA algorithm
    if use_cuda:
        astra_algo_conf = at.astra_dict('BP3D_CUDA')
    else:
        # TODO: slice into 2D forward projections
        raise NotImplementedError('No CPU 3D backprojection available.')

    astra_algo_conf['ReconstructionDataId'] = astra_vol_id
    astra_algo_conf['ProjectionDataId'] = astra_proj_id
    astra_algo_id = at.algorithm.create(astra_algo_conf)

    # Run it and remove afterwards
    at.algorithm.run(astra_algo_id)
    at.algorithm.delete(astra_algo_id)

    # Get the volume data and cycle back axes to translate from ASTRA axes
    # convention
    vol_fvals = at.data3d.get(astra_vol_id)
    vol_fvals = vol_fvals.swapaxes(0, 2).swapaxes(0, 1)

    # Create the volume grid function and return it
    vol_func = gf.Gfunc(vol_fvals, spacing=vol_grid.spacing)

    return vol_func
