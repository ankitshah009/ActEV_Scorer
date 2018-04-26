# actev_kernel_components.py
# Author(s): David Joy

# This software was developed by employees of the National Institute of
# Standards and Technology (NIST), an agency of the Federal
# Government. Pursuant to title 17 United States Code Section 105, works
# of NIST employees are not subject to copyright protection in the
# United States and are considered to be in the public
# domain. Permission to freely use, copy, modify, and distribute this
# software and its documentation without fee is hereby granted, provided
# that this notice and disclaimer of warranty appears in all copies.

# THE SOFTWARE IS PROVIDED 'AS IS' WITHOUT ANY WARRANTY OF ANY KIND,
# EITHER EXPRESSED, IMPLIED, OR STATUTORY, INCLUDING, BUT NOT LIMITED
# TO, ANY WARRANTY THAT THE SOFTWARE WILL CONFORM TO SPECIFICATIONS, ANY
# IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE, AND FREEDOM FROM INFRINGEMENT, AND ANY WARRANTY THAT THE
# DOCUMENTATION WILL CONFORM TO THE SOFTWARE, OR ANY WARRANTY THAT THE
# SOFTWARE WILL BE ERROR FREE. IN NO EVENT SHALL NIST BE LIABLE FOR ANY
# DAMAGES, INCLUDING, BUT NOT LIMITED TO, DIRECT, INDIRECT, SPECIAL OR
# CONSEQUENTIAL DAMAGES, ARISING OUT OF, RESULTING FROM, OR IN ANY WAY
# CONNECTED WITH THIS SOFTWARE, WHETHER OR NOT BASED UPON WARRANTY,
# CONTRACT, TORT, OR OTHERWISE, WHETHER OR NOT INJURY WAS SUSTAINED BY
# PERSONS OR PROPERTY OR OTHERWISE, AND WHETHER OR NOT LOSS WAS
# SUSTAINED FROM, OR AROSE OUT OF THE RESULTS OF, OR USE OF, THE
# SOFTWARE OR SERVICES PROVIDED HEREUNDER.

# Distributions of NIST software should also include copyright and
# licensing statements of any third-party software that are legally
# bundled with the code in compliance with the conditions of those
# licenses.

from metrics import *
from activity_instance import *
from sparse_signal import SparseSignal as S
from helpers import *
from alignment import *

from operator import add

# Returns true if there's some temporal intersection between the
# system and reference activity instances
def temporal_intersection_filter(r, s):
    ti = temporal_intersection(r, s)
    return (temporal_intersection(r, s) > 0, { "temporal_intersection": ti })

def build_temporal_overlap_filter(threshold):
    def _filter(r, s):
        tiou = temporal_intersection_over_union(r, s)
        return (tiou > threshold, { "temporal_intersection-over-union": tiou })

    return _filter

def temporal_intersection_over_union_component(r, s, cache):
    return { "temporal_intersection-over-union": cache.get("temporal_intersection-over-union", temporal_intersection_over_union(r, s)) }

def build_spatial_overlap_filter(threshold):
    def _filter(r, s):
        siou = spatial_intersection_over_union(r, s)
        return (siou > threshold, { "spatial_intersection-over-union": siou })

    return _filter

def build_simple_spatial_overlap_filter(threshold):
    def _filter(r, s):
        ssiou = simple_spatial_intersection_over_union(r.spatial_signal, s.spatial_signal)
        return (ssiou > threshold, { "spatial_intersection-over-union": ssiou })

    return _filter

def simple_spatial_intersection_over_union_component(r, s, cache):
    return { "spatial_intersection-over-union": cache.get("spatial_intersection-over-union", simple_spatial_intersection_over_union(r.spatial_signal, s.spatial_signal)) }

def object_type_match_filter(r, s):
    return (r.objectType == s.objectType, {})

def build_equiv_class_type_match_filter(classes):
    def _filter(r, s):
        r_class = classes.get(r.objectType, None)
        s_class = classes.get(s.objectType, None)

        if r_class is None or s_class is None:
            return (False, {})
        else:
            return (r_class == s_class, {})

    return _filter

def _object_signals_to_lookup(temporal_signal, local_objects):
    # Re-using the same empty ObjectLocalizationFrame, this is OK, as
    # long as we don't mutate it somewhere along the way
    empty_olf = ObjectLocalizationFrame.empty()

    def _r(init, o):
        selected_o = [ x for x in temporal_signal.join(o, lambda a, b: b if a else empty_olf).on_steps(lambda x: len(x.spatial_signal) > 0) ]

        init.extend(selected_o)
        return init

    return group_by_func(lambda x: x[0], reduce(_r, local_objects, []), lambda x: x[1])

# Using a factory function here so we can configure the object kernel
# at the protocol level.  Not sure if this is the best place to pass
# in the weightning functions
def build_object_congruence_filter(obj_kernel_builder, ref_filter, sys_filter, threshold, object_types = [], cmiss = lambda x: 1 * x, cfa = lambda x: 1 * x, target_rfas = [ 0.5, 0.2, 0.1, 0.033 ]):
    def _filter(r, s):
        components = _object_congruence(r, s, obj_kernel_builder, ref_filter, sys_filter, object_types, cmiss, cfa, target_rfas)
        obj_congruence = components["object_congruence"]

        return (False if obj_congruence is None else obj_congruence >= threshold, components)

    return _filter

def build_object_congruence(obj_kernel_builder, ref_filter, sys_filter, object_types = [], cmiss = lambda x: 1 * x, cfa = lambda x: 1 * x, target_rfas = [ 0.5, 0.2, 0.1, 0.033 ]):
    def object_congruence_component(r, s, cache):
        def _r_out_dict(init, k):
            ok, d = init
            still_ok = False
            if k in cache:
                d[k] = cache[k]
                still_ok = True

            return (ok and still_ok, d)

        was_cached, components = reduce(_r_out_dict, [ "object_congruence", "minMODE", "MODE_records", "alignment_records", "det_points", "ref_filter_localization" ] + [ "object-p_miss@{}rfa".format(target_rfa) for target_rfa in target_rfas ], (True, {}))

        if was_cached:
            return components
        else:
            return _object_congruence(r, s, obj_kernel_builder, ref_filter, sys_filter, object_types, cmiss, cfa, target_rfas)

    return object_congruence_component

def intersection_filter(r, s):
    return r & s

def ref_passthrough_filter(r, s):
    return r

def sys_passthrough_filter(r, s):
    return s

def _object_congruence(r, s, obj_kernel_builder, ref_filter, sys_filter, object_types, cmiss, cfa, target_rfas):
    # If object_types provided as non-empty array, only consider objects included in object_types
    ro = r.objects if len(object_types) == 0 else filter(lambda o: o.objectType in object_types, r.objects)
    so = s.objects if len(object_types) == 0 else filter(lambda o: o.objectType in object_types, s.objects)

    # For N_MODE computation, localizations spanning multiple files
    # are treated independently
    total_c, total_m, total_f, total_r = [], [], [], []
    frame_alignment_records = []
    # We need to report out the ref filter localization for aggregate
    # PMiss@RFA measurements
    ref_filter_localization = {}
    for r, s, k in temporal_signal_pairs(r, s):
        local_so_localizations = map(lambda o: o.localization.get(k, S()), so)
        local_ro_localizations = map(lambda o: o.localization.get(k, S()), ro)

        sos_lookup = _object_signals_to_lookup(sys_filter(r, s), local_so_localizations)
        ros_lookup = _object_signals_to_lookup(ref_filter(r, s), local_ro_localizations)

        ref_filter_localization[k] = ref_filter(r, s)

        for frame in sos_lookup.viewkeys() | ros_lookup.viewkeys():
            sys = sos_lookup.get(frame, [])
            ref = ros_lookup.get(frame, [])

            c, m, f = perform_alignment(ref, sys, obj_kernel_builder(sys))
            total_c.extend(c)
            total_m.extend(m)
            total_f.extend(f)
            total_r.extend(ref)

            for ar in c + m + f:
                frame_alignment_records.append((frame, ar))

    num_miss = len(total_m)
    num_correct = len(total_c)

    ref_filter_area = sum([ v.area() for v in ref_filter_localization.values() ])

    def _conf_sweep_reducer(init, conf):
        mode_scores, det_points = init
        num_filtered_c = len(filter(lambda ar: ar.sys.presenceConf >= conf, total_c))
        num_filtered_fa = len(filter(lambda ar: ar.sys.presenceConf >= conf, total_f))
        num_miss_w_filtered_c = num_miss + num_correct - num_filtered_c

        # Don't attempt to compute mode if there are no reference
        # objects
        if num_miss + num_correct > 0:
            mode_scores.append((conf, mode(num_filtered_c, num_miss_w_filtered_c, num_filtered_fa, cmiss, cfa)))

        det_points.append((conf, r_fa(num_filtered_c, num_miss_w_filtered_c, num_filtered_fa, ref_filter_area), p_miss(num_filtered_c, num_miss_w_filtered_c, num_filtered_fa)))

        return init

    mode_scores, det_points = reduce(_conf_sweep_reducer, sorted(list({ ar.sys.presenceConf for ar in total_c + total_f })), ([], []))

    min_mode = min(map(lambda x: x[1], mode_scores)) if len(mode_scores) > 0 else None
    pmiss_at_rfa_measures = { "object-p_miss@{}rfa".format(target_rfa): p_miss_at_r_fa(det_points, target_rfa) for target_rfa in target_rfas }

    out_components = { "object_congruence": 1 - min_mode if min_mode is not None else None,
                       "minMODE": min_mode,
                       "MODE_records": mode_scores,
                       "alignment_records": frame_alignment_records,
                       "det_points": det_points,
                       "ref_filter_localization": ref_filter_localization }

    return merge_dicts(out_components, pmiss_at_rfa_measures)
