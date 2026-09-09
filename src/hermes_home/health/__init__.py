"""Camera health monitoring and historical coverage.

Two questions that look alike and are not:

``spatial.py``   *does a camera point at this zone?*  -- static field of view
``health/``      *was that camera working at 3am?*    -- operational history

Conflating them is how "no events in the backyard" becomes an all-clear when
the camera was unplugged. They are kept in separate modules, reported in
separate fields, and never merged into one number.
"""
