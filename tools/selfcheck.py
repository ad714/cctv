import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import panels
import runtime
from events import EventStore

store = EventStore(os.path.join(tempfile.mkdtemp(), 'events.db'))
start = datetime(2026, 1, 1, 10, 0, 0)
first, new = store.record(2, start, 'human')
assert new
same, new = store.record(2, start + timedelta(seconds=5), 'vehicle')
assert same == first and not new
assert store.get(first).target == 'human,vehicle' and store.get(first).hits == 2
other, new = store.record(2, start + timedelta(seconds=40), 'human')
assert new and other != first
assert store.unread_counts() == {2: 2}
store.mark_seen([first])
assert store.unread_counts() == {2: 1}
assert [e.id for e in store.recent(kind='vehicle')] == [first]
assert [e.id for e in store.recent(unread=True)] == [other]

mark = store.add_bookmark(2, start + timedelta(minutes=5))
store.set_bookmark_note(mark, 'gate left open')
assert store.bookmarks(2, start.date()) == [(mark, start + timedelta(minutes=5), 'gate left open')]
store.delete_bookmark(mark)
assert store.bookmarks(2, start.date()) == []

alerts = dict(panels.DEFAULT_ALERTS)
assert panels.alert_allowed(alerts, 1, 'human', '12:00')
assert not panels.alert_allowed(dict(alerts, muted=[1]), 1, 'human', '12:00')
assert not panels.alert_allowed(dict(alerts, kind='vehicle'), 1, 'human', '12:00')
assert panels.alert_allowed(dict(alerts, kind='vehicle'), 1, 'human,vehicle', '12:00')
night = dict(alerts, quiet=True, quiet_from='23:00', quiet_to='06:00')
assert not panels.alert_allowed(night, 1, 'human', '23:30')
assert not panels.alert_allowed(night, 1, 'human', '05:59')
assert panels.alert_allowed(night, 1, 'human', '06:00')

assert runtime.atempo_chain(4.0) == 'atempo=2.0,atempo=2.0000'
assert runtime.atempo_chain(0.25) == 'atempo=0.5,atempo=0.5000'
assert runtime.denoise_filter(0) == ''
assert panels.describe('clip-ch3-20261124-150923-150936.mp4')[1].startswith('Clip\nCamera 3')
print('selfcheck ok')
