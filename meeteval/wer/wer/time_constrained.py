"""
This file contains the time-constrained minimum permutation word error rate
"""
import itertools
import typing
from dataclasses import dataclass

from meeteval.io.stm import STM
from meeteval.wer.wer.error_rate import ErrorRate
from meeteval.wer.wer.cp import CPErrorRate
from typing import List, Dict

from meeteval.wer.utils import _values, _map, _items

if typing.TYPE_CHECKING:
    from meeteval._typing import TypedDict


    class Segment(TypedDict):
        words: str
        start_time: 'int | float'
        end_time: 'int | float'
        speaker: typing.Optional[str]

__all__ = ['time_constrained_minimum_permutation_word_error_rate', 'time_constrained_siso_word_error_rate']


# pseudo-timestamp strategies
def equidistant_intervals(interval, words):
    """Divides the interval into `count` equally sized intervals"""
    count = len(words)
    if count == 0:
        return []
    elif count == 1:
        return [interval]
    interval_length = (interval[1] - interval[0]) / count
    return [(interval[0] + i * interval_length, interval[0] + (i + 1) * interval_length) for i in range(count)]


def equidistant_points(interval, words):
    """Places `count` points (intervals of size zero) in `interval` with equal distance"""
    count = len(words)
    if count == 0:
        return []
    elif count == 1:
        return [((interval[0] + interval[1]) / 2,) * 2]
    interval_length = (interval[1] - interval[0]) / count

    return [(interval[0] + (i + 0.5) * interval_length,) * 2 for i in range(count)]


def character_based(interval, words):
    if len(words) == 0:
        return []
    elif len(words) == 1:
        return [interval]
    import numpy as np
    word_lengths = np.asarray([len(w) for w in words])
    end_points = np.cumsum(word_lengths)
    total_num_characters = end_points[-1]
    character_length = (interval[1] - interval[0]) / total_num_characters
    return [(interval[0] + character_length * start, interval[0] + character_length * end) for start, end in
            zip([0] + list(end_points[:-1]), end_points)]


def full_segment(interval, words):
    """Outputs `interval` for each word"""
    return [interval] * len(words)


pseudo_word_level_strategies = dict(
    equidistant_intervals=equidistant_intervals,
    equidistant_points=equidistant_points,
    full_segment=full_segment,
    character_based=character_based
)


def _check_timing_annotations(t, k):
    import numpy as np
    t = np.array(t)
    if np.any(t[1:, 0] < t[:-1, 0]):
        raise ValueError(f'The time annotations must be sorted by start time')
    overlaps = []
    for s1, s2 in zip(t[1:], t[:-1]):
        if s1[0] < s2[1] and s2[0] < s1[1]:
            overlaps.append((s1, s2))
    if len(overlaps):
        import warnings
        k = k or 'unknown'
        warnings.warn(
            f'A speaker ({k}) overlaps with itself. '
            f'This can lead to contradictions between pseudo-word-level timings and word order. '
            f'An exception will be raised later when such a contradiction occurs. '
        )


def _time_constrained_siso_error_rate(
        reference, hypothesis, reference_timing, hypothesis_timing
):
    from meeteval.wer.matching.cy_levenshtein import time_constrained_levenshtein_distance_with_alignment

    if len(reference) == 0 or isinstance(reference[0], int):
        eps = max(*reference, *hypothesis) + 1
    else:
        eps = '*'
    result = time_constrained_levenshtein_distance_with_alignment(
        reference, hypothesis, reference_timing, hypothesis_timing,
        eps=eps
    )

    return ErrorRate(
        result['total'],
        len(reference),
        insertions=result['insertions'],
        deletions=result['deletions'],
        substitutions=result['substitutions'],
    )


@dataclass
class TimeMarkedTranscript:
    transcript: List[str]
    timings: List[typing.Tuple[float, float]]

    def check(self, msg):
        _check_timing_annotations(self.timings, msg)

    def has_self_overlaps(self):
        # Timings are sorted by start time, so it is enough to check
        # that the end time of each segment is not smaller
        # than the start time of the next segment
        last_end = 0
        for t in self.timings:
            if last_end > t[0]:
                return True
            last_end = t[1]
        return False

    @classmethod
    def create(cls, data) -> 'TimeMarkedTranscript':
        if isinstance(data, TimeMarkedTranscript):
            return data
        elif isinstance(data, STM):
            return cls.from_stm(data)
        elif isinstance(data, list) and isinstance(data[0], dict):
            return cls.from_segment_dicts(data)
        else:
            raise TypeError(data)

    @classmethod
    def from_stm(cls, stm: STM) -> 'TimeMarkedTranscript':
        speaker_ids = stm.grouped_by_speaker_id().keys()
        assert len(speaker_ids) == 1, 'Only single-speaker STMs are supported'
        stm = stm.sorted_by_begin_time()
        time_marked_transcript = cls(
            [l.transcript for l in stm.lines],
            [(l.begin_time, l.end_time) for l in stm.lines],
        )
        time_marked_transcript.check(next(iter(speaker_ids)))
        return time_marked_transcript

    @classmethod
    def from_segment_dicts(cls, data: 'List[Segment]') -> 'TimeMarkedTranscript':
        if 'speaker' in data[0]:
            assert all(d['speaker'] == data[0]['speaker'] for d in data[1:])
        time_marked_transcript = cls(
            transcript=[s['words'] for s in data],
            timings=[(s['start_time'], s['end_time']) for s in data],
        )
        time_marked_transcript.check(None)
        return time_marked_transcript


def get_pseudo_word_level_timings(
        s: TimeMarkedTranscript,
        strategy: str,
        collar: float = 0,
) -> TimeMarkedTranscript:
    pseudo_word_level_strategy = pseudo_word_level_strategies[strategy]

    all_words = []
    word_level_timings = []

    for words, interval in zip(s.transcript, s.timings):
        words = words.split()  # Get words form segment
        segment_timings = pseudo_word_level_strategy(interval, words)
        word_level_timings.extend(segment_timings)
        all_words.extend(words)
        assert len(words) == len(segment_timings), (words, segment_timings)

    word_level_timings = [(max(t[0] - collar, 0), t[1] + collar) for t in word_level_timings]
    return TimeMarkedTranscript(all_words, word_level_timings)


def remove_overlaps(
        s: TimeMarkedTranscript,
        max_overlap: float = 0.4,
        warn_message: str = None,
) -> TimeMarkedTranscript:
    """
    Remove overlaps between words or segments in a transcript.

    Args:
        s: TimeMarkedTranscript
        max_overlap: maximum allowed relative overlap between words or segments.
            Raises a `ValueError` when more overlap is found.
        warn_message: if not None, a warning is printed when overlaps are corrected.
    """
    corrected_timings = []
    for t in s.timings:
        if corrected_timings and corrected_timings[-1][1] > t[0]:
            if warn_message is not None:
                import warnings
                warnings.warn(warn_message)
            last = corrected_timings[-1]
            overlap = last[1] - t[0]
            if overlap > max_overlap * (t[1] - last[0]):
                import numpy as np
                raise ValueError(
                    f'Overlapping segments exceed max allowed relative overlap. '
                    f'Segment {last} overlaps with {t}. '
                    f'{overlap} > {max_overlap * (t[1] - last[0])} '
                    f'relative overlap: {np.divide(overlap, (t[1] - last[-1]))}'
                )
            center = (last[-1] + t[0]) / 2
            corrected_timings[-1] = (last[0], center)
            t = (center, t[1])

            assert t[1] > t[0], t
            assert last[1] > last[0], last

        corrected_timings.append(t)
    return TimeMarkedTranscript(s.transcript, corrected_timings)


def sort_segments(s: TimeMarkedTranscript):
    import numpy as np
    order = np.argsort(np.asarray(s.timings)[:, 0])
    return TimeMarkedTranscript(
        [s.transcript[int(i)] for i in order],
        [s.timings[int(i)] for i in order],
    )


def time_constrained_siso_word_error_rate(
        reference: TimeMarkedTranscript,
        hypothesis: TimeMarkedTranscript,
        reference_pseudo_word_level_timing='full_segment',
        hypothesis_pseudo_word_level_timing='equidistant_intervals',
        collar: int = 0,
):
    """
    Time-constrained word error rate for single-speaker transcripts.

    Args:
        reference: reference transcript
        hypothesis: hypothesis transcript
        reference_pseudo_word_level_timing: strategy for pseudo-word level timing for reference
        hypothesis_pseudo_word_level_timing: strategy for pseudo-word level timing for hypothesis
        collar: collar applied to hypothesis pseudo-word level timings
    """
    reference = get_pseudo_word_level_timings(reference, reference_pseudo_word_level_timing)
    hypothesis = get_pseudo_word_level_timings(hypothesis, hypothesis_pseudo_word_level_timing, collar)
    return _time_constrained_siso_error_rate(
        reference.transcript, hypothesis.transcript,
        reference.timings, hypothesis.timings,
    )


def time_constrained_minimum_permutation_word_error_rate(
        reference: 'List[TimeMarkedTranscript] | Dict[str, TimeMarkedTranscript]',
        hypothesis: 'List[TimeMarkedTranscript] | Dict[str, TimeMarkedTranscript]',
        reference_pseudo_word_level_timing='full_segment',
        hypothesis_pseudo_word_level_timing='equidistant_intervals',
        collar: int = 0,
        reference_overlap_correction=False,
        hypothesis_allow_speaker_self_overlap=False,
) -> CPErrorRate:
    """
    Time-constrained minimum permutation word error rate for single-speaker transcripts.

    Args:
        reference: reference transcript
        hypothesis: hypothesis transcript
        reference_pseudo_word_level_timing: strategy for pseudo-word level timing for reference
        hypothesis_pseudo_word_level_timing: strategy for pseudo-word level timing for hypothesis
        collar: collar applied to hypothesis pseudo-word level timings
        reference_overlap_correction: if True, overlaps in the reference are corrected
        hypothesis_allow_speaker_self_overlap: if True, overlaps in the hypothesis are allowed.
            This can change the order of words, so it is not recommended to use this option.
            It is not guaranteed that the returned WER is correct if this option is set!
    """
    from meeteval.wer.matching.cy_levenshtein import time_constrained_levenshtein_distance_v3
    from meeteval.wer.wer.cp import _cp_word_error_rate

    reference = _map(TimeMarkedTranscript.create, reference)
    hypothesis = _map(TimeMarkedTranscript.create, hypothesis)
    print(reference)

    if reference_overlap_correction:
        reference = _map(
            lambda x: remove_overlaps(
                x,
                warn_message='A speaker overlaps with itself in the reference! This is likely '
                             'caused by numerical errors during reference creation and '
                             'corrected by MeetEval by shifting the boundaries to the center '
                             'point of the overlapping region.'
            ),
            reference
        )

    # Convert segments into lists of words and word-level timings
    reference = _map(lambda x: get_pseudo_word_level_timings(x, reference_pseudo_word_level_timing),
                     reference)
    hypothesis = _map(lambda x: get_pseudo_word_level_timings(x, hypothesis_pseudo_word_level_timing, collar),
                      hypothesis)

    if hypothesis_allow_speaker_self_overlap:
        def check_self_overlap(x):
            if x.has_self_overlaps():
                import warnings
                warnings.warn(
                    'A speaker overlaps with itself in the hypothesis! This is ignored because the '
                    'hypothesis_allow_speaker_self_overlap option is set. '
                    'It is not guaranteed that the WER is correct!'
                )
        _map(check_self_overlap, hypothesis)
        hypothesis = _map(sort_segments, hypothesis)

    sym2int = {v: i for i, v in enumerate({
        word for words in itertools.chain(_values(reference), _values(hypothesis)) for word in words.transcript
    })}

    reference = _map(lambda x: TimeMarkedTranscript([sym2int[s] for s in x.transcript], x.timings), reference)
    hypothesis = _map(lambda x: TimeMarkedTranscript([sym2int[s] for s in x.transcript], x.timings), hypothesis)

    return _cp_word_error_rate(
        reference, hypothesis,
        distance_fn=lambda tt, et: time_constrained_levenshtein_distance_v3(
            tt.transcript, et.transcript, tt.timings, et.timings
        ),
        siso_error_rate=lambda tt, et: _time_constrained_siso_error_rate(
            tt.transcript, et.transcript, tt.timings, et.timings
        ),
        missing=TimeMarkedTranscript([], []),
    )
