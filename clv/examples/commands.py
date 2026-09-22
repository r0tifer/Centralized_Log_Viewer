"""Two commands: one that acts on the view, one that opens a panel.

A worked ``Command`` example. Drop this file in ``~/.config/clv/plugins/``
(CLV puts it in ``examples/`` for you) and add it to ``settings.conf``::

    [log_viewer]
    plugins = commands

    [plugin:commands]
    # The key for "Show errors only". Unset, the command has no key and is run
    # by name from C. A key CLV already uses is refused and reported.
    errors_key = e
    # Where "Save this view" writes. Unset, the panel opens with the field
    # empty and nothing is written until you fill it in.
    save_to = /tmp/clv-view.txt

It adds the one thing every other seam in CLV lacks: a way to be *asked*. A
format is consulted when a line arrives, a metric when a bucket is drawn, a
matcher when a rule is tested — none of them can be invoked. A command is run
because somebody pressed a key or picked it out of ``C``.

Six things are worth copying out of this file.

**You cannot reach the app, and that is not a formality.** ``CommandContext``
is data in both directions: ``notify`` and the three ``request_*`` methods
append to a queue CLV drains after you return. There is no object here with a
``screen`` on it, no callable that closes over one, and nothing you can walk to
get at one. So the contract "a command cannot drive CLV" is a fact about the
type rather than a rule you are trusted to keep.

**Ask for state changes; do not assume them.** ``request_query`` is *asking*.
If the query does not parse, CLV refuses it, tells the operator which plugin
asked, and leaves the view alone. ``ShowErrors`` below does not check whether
its query is valid, because CLV checking is the whole point of asking.

**Your key is hidden, and it may be refused.** Whatever you set, the binding is
installed with ``show=False``: the footer is hand-tuned against an 80-column
floor and a plugin cannot know what its entry would push off. If the key you
asked for is one of CLV's, or one another command claimed first, you are told
and you keep working — ``C`` lists you by name, which is why that dialog exists.

**You run on the event loop, synchronously.** Nothing else in CLV happens while
``run`` is executing. ``SaveView`` writes a file, which is fast; a command that
talks to a network should do that on a thread of its own and answer from what
the thread last stored, exactly as an annotation provider does.

**A panel is described, not built.** You return a ``Panel`` of ``Control``
descriptions and CLV decides what they look like — which is what keeps every
breakpoint test in CLV independent of what is installed. You never import a
widget, you never ship CSS, and your panel is readable at 80 columns because
CLV's are.

**``on_control`` is on a keystroke path.** It is called for every character
typed into an input, so it is charged against ``plugin_time_budget_ms`` like
anything else CLV calls from a render. ``SaveView`` does nothing in it until a
button is pressed, which is the shape to copy: read ``context.values`` when
something is actually being asked of you, not on the way past.
"""

from __future__ import annotations

from pathlib import Path

from clv.api import Command, CommandContext, Control, Panel


class ShowErrors(Command):
    """Set the query to errors-and-worse, by asking CLV to do it.

    The whole command is one ``request_query``. It is worth reading for what it
    does *not* do: it does not touch the query box, does not re-render, does not
    validate the query itself, and does not find out whether it worked. All four
    are CLV's, and a plugin that did any of them would be a plugin that could
    get them wrong.
    """

    name = "show-errors"
    command_name = "show-errors"
    title = "Show errors only"

    #: Read from ``[plugin:commands]`` at configure time. A key is a scarce,
    #: shared resource -- there are twenty-odd single characters and CLV has
    #: most of them -- so the operator picks it rather than the author.
    key = ""

    def configure(self, settings):
        # Kept as a live view, not copied out of: editing the settings file
        # changes what this reads without a restart. Read at `configure` rather
        # than in `run`, because `key` is consulted once, at load, when the
        # binding is installed.
        self._settings = settings
        self.key = settings.get("errors_key", "").strip()

    def run(self, context: CommandContext) -> None:
        context.request_query("level>=error")
        # Said out loud, because a query that applies silently looks the same as
        # one that was refused. If CLV refuses it, the operator gets *that*
        # message too, naming this plugin -- so the two never contradict.
        context.notify("Showing errors and worse")


class SaveView(Command):
    """Write the filtered lines to a file the operator picks in a panel.

    The panel is the point. A command that needed a destination before this seam
    existed had two choices: hardcode one, or make the operator edit
    ``settings.conf`` and restart. Neither is a thing you would do to somebody
    twice.
    """

    name = "save-view"
    command_name = "save-view"
    title = "Save this view to a file"

    def configure(self, settings):
        self._settings = settings

    def run(self, context: CommandContext) -> Panel:
        # Every value the panel opens with comes from somewhere the operator can
        # see: the settings file, or the count of what they are looking at.
        return Panel(
            title="Save this view",
            controls=(
                Control(
                    kind="static",
                    id="summary",
                    label=f"{len(context.entries)} lines match the current filters.",
                ),
                Control(
                    kind="input",
                    id="path",
                    label="Write to",
                    value=self._settings.get("save_to", ""),
                    placeholder="/tmp/clv-view.txt",
                ),
                Control(
                    kind="switch",
                    id="raw",
                    label="Raw lines (off writes timestamp, level, message)",
                    value=False,
                ),
                Control(kind="button", id="save", label="Save"),
            ),
        )

    def on_control(self, control_id: str, value, context: CommandContext):
        # Nothing happens until the button. `on_control` is called for every
        # character typed into `path`, and doing the work there would mean
        # writing a file per keystroke -- each one to a half-typed name.
        if control_id != "save":
            return None

        destination = str(context.values.get("path", "")).strip()
        if not destination:
            # Reported, and the panel stays open on the field that is wrong. A
            # plugin that dismissed here would be answering a mistake by
            # throwing away everything else the operator had filled in.
            context.notify("Give a path to write to", "warning")
            return None

        raw = bool(context.values.get("raw", False))
        lines = [
            entry.raw
            if raw
            else f"{entry.timestamp or ''}\t{entry.level or ''}\t{entry.message}"
            for entry in context.entries
        ]
        try:
            Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            # Caught rather than raised. Raising takes this command out of
            # service for the whole session over a typo in a path -- correct for
            # a plugin that is broken, and much too harsh for one that was
            # pointed at a directory that does not exist.
            context.notify(f"Could not write {destination}: {exc}", "error")
            return None

        context.notify(f"Wrote {len(lines)} lines to {destination}")
        # Closes the panel. The work is done and there is nothing left to ask.
        return Panel(dismiss=True)
