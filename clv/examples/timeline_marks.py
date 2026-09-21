"""Marks on the timeline's axis, and a bucket that measures bytes.

A worked ``TimelineAnnotation`` and ``TimelineMetric`` example. Drop this file
in ``~/.config/clv/plugins/`` (CLV puts it in ``examples/`` for you) and add it
to ``settings.conf``::

    [log_viewer]
    plugins = timeline_marks

    [plugin:timeline_marks]
    # When the deploys happened, as ISO timestamps. Unset, the provider marks
    # nothing and only the metric below is doing any work.
    deploys = 2026-08-07 09:14:20, 2026-08-07 09:41:05
    # What each one was, in the same order. Short: it goes in a caption.
    deploy_labels = api v4.2, api v4.3
    # Which field carries a size, for the metric. Unset, the metric measures
    # the length of the line itself.
    size_field = bytes

It adds two things the histogram (`b`) could not do before.

A spike gets a cause
    A bar showing five hundred errors at 09:14 is a question. The same bar with
    ``deploy api v4.2`` marked on that bucket is an answer, and the difference
    is one line in a config file rather than a second window open beside CLV.

A bucket stops meaning "lines"
    A hundred lines is not a hundred kilobytes. On a log where the interesting
    quantity is *volume of data* — an access log, a transfer log, anything with
    a size field — a histogram of line counts is a histogram of the wrong
    thing, and the shape of the two can differ completely.

Five things are worth copying out of this file.

**An annotation provider may not do I/O, and this one does not.** ``annotations``
is called on the event loop, inside the render that draws the bar, and it is
charged against ``plugin_time_budget_ms`` like any other render-path plugin. So
the moments are parsed **once**, in ``configure``, and the call itself is a walk
over a list that is already in memory. A provider that wants deploys from an API
fetches them on a thread of its own and answers this call from whatever that
thread last stored — it does not fetch here.

**You are asked once per window, not once per render.** CLV caches your answer
against the window the bar is showing, so typing in the query box does not
re-ask you. Returning marks outside the window is harmless — they are dropped —
so filtering by ``window`` yourself is an optimisation, not a requirement. This
one does it anyway, because it is one comparison and it makes the caption's
``(+N more)`` mean what it says.

**A label goes in a caption, so keep it short.** The caption is one row under a
bar that is as wide as the pane. ``deploy api v4.2`` fits at 80 columns;
``Deployment of api-server version 4.2.1 by ci-runner-7`` does not, and what an
operator sees of it is the first few words followed by an ellipsis.

**The level is a severity, normalised the way a log line's is.** ``"notice"``
here is the same ``NOTICE`` a parsed line would carry, and it is what colours
the mark. Return ``None`` for something that is not a severity at all rather
than inventing one — a maintenance window is not a warning.

**A metric must be foldable, and CLV is what makes that true.** You return a
number per entry; CLV sums it. There is no ``aggregate()`` to implement, and
that is the interface doing its job: a median would be right on the first render
and wrong on every line tailed after it, because ``Timeline.extend`` folds an
arrival into a bucket by arithmetic. If what you want is a median, what you
actually want is a different feature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from clv.api import LogEntry, TimeWindow, TimelineAnnotation, TimelineMetric, setting_list


def _moment(text: str) -> Optional[datetime]:
    """One configured timestamp, or None if the operator mistyped it.

    A bad line in ``settings.conf`` is skipped rather than raised on. Raising
    from ``configure`` is contained — CLV takes the plugin out of service and
    says so — but one mistyped deploy should not take the other nine off the
    bar.
    """

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class DeployMarks(TimelineAnnotation):
    """Mark configured moments on the axis.

    **Inert until configured.** With no ``deploys`` set this marks nothing,
    which is the no-op answer: the bar is exactly what it would have been
    without this plugin installed. The same shape of consent the journal
    provider and the alert sink ship with.
    """

    name = "deploy-marks"

    def __init__(self) -> None:
        self._settings: dict = {}
        self._marks: list[tuple[datetime, str]] = []
        #: The raw strings the marks above were parsed from. Compared rather
        #: than counted: an operator who corrects a deploy's *time* leaves the
        #: number of them alone, and a plugin that reloaded only on a change of
        #: count would go on marking the old moment.
        self._parsed_from: tuple[str, str] = ("", "")

    def configure(self, settings) -> None:
        # Kept, not copied out of: CLV mutates the mapping behind this view when
        # it re-reads `settings.conf`, so reading through it is what lets an
        # edit take effect without a restart.
        self._settings = settings
        self._reload()

    def _reload(self) -> None:
        """Parse the configured moments once, here, and never in `annotations`.

        This is the whole discipline of the seam in one method. Everything
        expensive — parsing, sorting, reading anything at all — happens where an
        operator is waiting for a config file to be read. What happens on the
        render path is a comparison per mark.
        """

        self._parsed_from = self._raw()
        moments = [_moment(text) for text in setting_list(self._settings, "deploys")]
        labels = setting_list(self._settings, "deploy_labels")
        self._marks = sorted(
            (moment, labels[index] if index < len(labels) else "deploy")
            for index, moment in enumerate(moments)
            if moment is not None
        )

    def _raw(self) -> tuple[str, str]:
        """The two settings as written, which is what a reload keys on."""

        return (
            self._settings.get("deploys", ""),
            self._settings.get("deploy_labels", ""),
        )

    def annotations(
        self, window: TimeWindow
    ) -> Iterable[tuple[datetime, str, Optional[str]]]:
        # Checked every call, because `configure` is not the only thing that
        # changes these: CLV hands out a live view of the section, so an edit to
        # `settings.conf` shows up here without a second hook. Two dict reads
        # and a tuple compare; the parse they guard is the thing that is not.
        if self._raw() != self._parsed_from:
            self._reload()
        for moment, label in self._marks:
            if window.contains(moment):
                # `notice` rather than `info`: a deploy is not an event in the
                # log, it is a thing that explains the events in the log, and it
                # should not be the same colour as the quietest line on screen.
                yield (moment, label, "notice")


class BytesPerBucket(TimelineMetric):
    """Measure bytes rather than lines.

    Falls back to the length of the raw line when no ``size_field`` is
    configured, which makes the plugin useful on a log that carries no size at
    all — and demonstrates the more important point: what a metric measures is
    *your* decision about the operator's logs, and a metric with no sensible
    default should return ``None`` and measure nothing until it is told what to
    look at.
    """

    name = "bytes-metric"
    metric_name = "bytes"
    #: Printed after the scaled figure, so 1 449 984 reads as `1.4 MB`.
    unit = "B"

    def __init__(self) -> None:
        self._settings: dict = {}

    def configure(self, settings) -> None:
        self._settings = settings

    def value(self, entry: LogEntry) -> Optional[float]:
        # Per entry, per rebuild, memoised by nothing: this is the expensive
        # half of the seam and every line of it is on the render path. A dict
        # read, an int(), and nothing else.
        key = self._settings.get("size_field", "").strip()
        if not key:
            return float(len(entry.raw))
        raw = entry.fields.get(key)
        if raw is None:
            # Not measured by me. The entry is still counted -- `count` never
            # stops meaning entries -- and it contributes nothing to the sum,
            # which is what lets a metric answer for the lines it understands
            # and stay out of the way for the rest.
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            # A size field that is not a number on this line. Same answer, same
            # reason: a metric that raised here would be taken out of service by
            # one malformed line in a buffer of half a million.
            return None


__all__ = ["BytesPerBucket", "DeployMarks"]
