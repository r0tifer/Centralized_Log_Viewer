"""Worked plugin examples, shipped as source and seeded into the plugin directory.

Nothing here is imported by CLV. These modules exist to be *read* and to be
copied: :func:`clv.services.config.ensure_user_plugin_dir` writes them into
``~/.config/clv/plugins/`` on first run, where they behave exactly as any other
user plugin does — listed, inert, and run only once the operator names them in
``settings.conf``.

They live outside ``clv/plugins/`` on purpose. Everything under that package is
walked by the bundled drop-in loader and loads *without* being named, on the
argument that trusting a plugin CLV shipped is the trust already placed in CLV.
An example is not that: it is a demonstration the operator opts into, and the
plugin count should go on meaning "plugins someone installed".
"""
