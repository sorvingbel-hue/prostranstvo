"""Original pixel-art Creeper portrait for the local daily-plan guide."""

import math
import time
import tkinter as tk


class CreeperPortrait(tk.Canvas):
    SIZE = 144
    FRAME_MS = 65

    def __init__(self, parent):
        super().__init__(parent, width=self.SIZE, height=self.SIZE,
                         background='#dfefdc', highlightthickness=0, takefocus=False)
        self._timer = None
        self._speaking = False
        self._offset = 0
        self._started = time.monotonic()
        self._draw_portrait()
        self.bind('<Map>', self._start, add='+')
        self.bind('<Unmap>', self._stop, add='+')
        self.bind('<Destroy>', self._stop, add='+')

    def set_speaking(self, speaking):
        """Select the talking pose while the guide is speaking."""
        self._speaking = bool(speaking)
        self._pose(time.monotonic() - self._started)

    def _box(self, x, y, width, height, color, *tags):
        return self.create_rectangle(x, y, x + width, y + height, fill=color,
                                     width=0, tags=tags)

    def _surface(self, coords, color):
        self.create_polygon(*coords, fill=color, outline='', tags=('creeper',))

    def _draw_portrait(self):
        for x, y, size, color in (
            (11, 21, 10, '#edf6e9'), (121, 21, 8, '#bddcb6'),
            (10, 77, 7, '#bad8b3'), (124, 64, 12, '#ebf5e8'),
            (110, 105, 9, '#cce5c6'), (23, 112, 5, '#eef6eb'),
            (115, 12, 4, '#edf6e9'),
        ):
            self._box(x, y, size, size, color)
        self.create_oval(32, 123, 115, 136, fill='#c7dfc1', outline='')

        # Vertical torso and its darker side. The portrait ends at the torso.
        self._surface((53, 75, 95, 75, 106, 70, 106, 120, 95, 128, 53, 128), '#31583a')
        self._box(56, 77, 37, 48, '#6baf5d', 'creeper')
        self._surface((94, 77, 103, 73, 103, 119, 94, 125), '#4b8350')
        for x, y, width, height, color in (
            (57, 83, 12, 8, '#80bb6d'), (70, 91, 9, 12, '#53944f'),
            (82, 84, 10, 11, '#94c676'), (56, 104, 10, 12, '#579750'),
            (72, 109, 14, 8, '#80ba69'), (85, 117, 7, 7, '#52884f'),
        ):
            self._box(x, y, width, height, color, 'creeper')

        # Square head with a shallow top and right face.
        self._surface((31, 26, 46, 16, 111, 16, 111, 75, 96, 86, 31, 86), '#2d5235')
        self._surface((35, 26, 48, 19, 108, 19, 95, 26), '#a3ce7e')
        self._surface((97, 29, 108, 22, 108, 73, 97, 81), '#558a4f')
        self._box(34, 29, 61, 54, '#79b864', 'creeper')
        for x, y, width, height, color in (
            (34, 29, 14, 11, '#93c879'), (49, 30, 10, 8, '#60a656'),
            (61, 29, 13, 11, '#a0cb75'), (82, 34, 11, 10, '#639e53'),
            (35, 48, 11, 13, '#6bad60'), (47, 55, 9, 8, '#9bc878'),
            (64, 46, 9, 13, '#93c576'), (84, 53, 11, 10, '#8ac071'),
            (37, 70, 12, 10, '#97c774'), (50, 73, 11, 10, '#5a9e53'),
            (72, 74, 8, 9, '#68a756'), (85, 71, 10, 12, '#548f4b'),
            (100, 41, 7, 10, '#4e7d45'), (98, 60, 7, 8, '#65975a'),
        ):
            self._box(x, y, width, height, color, 'creeper')

        ink = '#19332a'
        for x in (46, 74):
            self._box(x, 44, 12, 12, ink, 'creeper', 'eyes-open')
            self._box(x + 2, 45, 3, 3, '#d7f0bc', 'creeper', 'eyes-open')
            self._box(x, 51, 12, 3, ink, 'creeper', 'eyes-closed')
        self._box(46, 59, 6, 3, '#dbcc85', 'creeper')
        self._box(80, 59, 6, 3, '#dbcc85', 'creeper')
        self._box(58, 56, 16, 6, ink, 'creeper')
        for x in (54, 72):
            self._box(x, 62, 6, 9, ink, 'creeper')
        self._box(60, 62, 12, 4, ink, 'creeper', 'mouth-idle')
        self._box(60, 62, 12, 10, ink, 'creeper', 'mouth-talk')
        self._box(62, 68, 8, 2, '#8bbb6b', 'creeper', 'mouth-talk')
        self._pose(0)

    def _pose(self, elapsed):
        offset = round(2 * math.sin(elapsed * math.tau / 2.4))
        self.move('creeper', 0, offset - self._offset)
        self._offset = offset
        blink = elapsed % 4.3 > 4.1
        talk = self._speaking and int(elapsed * 8) % 2 == 0
        self.itemconfigure('eyes-open', state='hidden' if blink else 'normal')
        self.itemconfigure('eyes-closed', state='normal' if blink else 'hidden')
        self.itemconfigure('mouth-idle', state='hidden' if talk else 'normal')
        self.itemconfigure('mouth-talk', state='normal' if talk else 'hidden')

    def _start(self, _event):
        if self._timer is None:
            self._tick()

    def _tick(self):
        self._pose(time.monotonic() - self._started)
        self._timer = self.after(self.FRAME_MS, self._tick)

    def _stop(self, _event):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
