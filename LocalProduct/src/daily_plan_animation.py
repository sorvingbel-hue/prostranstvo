"""Pixel-art mining scene and weighted cabinet for the daily checklist."""

import math
import time
import tkinter as tk

from daily_plan import REBIRTH_POINTS, equipment_for_points


ARMOR_COLORS = {
    # Outline, shadow, main face, highlight.
    'leather': ('#4f342a', '#76503b', '#a9724f', '#c79064'),
    'copper': ('#5c382c', '#975034', '#c7794d', '#efa67a'),
    'iron': ('#555f69', '#909da6', '#c6d0d4', '#f3f7f6'),
    'gold': ('#8d6022', '#be882b', '#e4b847', '#fff1a0'),
    'diamond': ('#1c6a72', '#288f98', '#54c9c2', '#bcfff1'),
    'netherite': ('#272430', '#463d4d', '#605568', '#9b859d'),
}

PICKAXES = {
    # Russian label, dark edge, head, highlight.
    'iron': ('Железная кирка', '#545e64', '#bfcbd1', '#f5f7f6'),
    'diamond': ('Алмазная кирка', '#236c74', '#58d3cb', '#caffef'),
    'netherite': ('Незеритовая кирка', '#251e2a', '#63546a', '#a48fa9'),
}


class DailyPlanAnimation(tk.Canvas):
    WIDTH = 420
    HEIGHT = 132
    DURATION = 0.94
    FIRST_HIT = 0.22
    IMPACT = 0.43
    CABINET_X = 177
    CABINET_WIDTH = 231
    SLOT_LEFT = 184
    SLOT_RIGHT = 401
    SLOT_CENTER_Y = 66

    def __init__(self, parent):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT,
                         background='#d9edf0', highlightthickness=0)
        self._tasks = ()
        self._points = 0
        self._animated_task = None
        self._started = 0.0
        self._after_id = None
        self.bind('<Configure>', self._on_resize, add='+')
        self.bind('<Destroy>', self._on_destroy, add='+')
        self._draw()

    def sync(self, tasks, points):
        """Display a snapshot without replaying prior completions."""
        state = (tuple(tasks), points)
        if state == (self._tasks, self._points):
            return
        self._cancel()
        self._tasks, self._points = state
        self._draw()

    def celebrate(self, tasks, points, completed_task_id):
        """Animate a newly completed task into its own section."""
        self._cancel()
        self._tasks, self._points = tuple(tasks), points
        if not any(task_id == completed_task_id and completed
                   for task_id, _, completed in self._tasks):
            self._draw()
            return
        self._animated_task = completed_task_id
        self._started = time.monotonic()
        self._tick()

    def _tick(self):
        self._after_id = None
        fraction = (time.monotonic() - self._started) / self.DURATION
        if fraction >= 1:
            self._animated_task = None
            self._draw()
            return
        self._draw(fraction)
        self._after_id = self.after(25, self._tick)

    def _cancel(self):
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        self._animated_task = None

    def _on_resize(self, _event):
        fraction = None
        if self._animated_task is not None:
            fraction = min(1.0, (time.monotonic() - self._started) / self.DURATION)
        self._draw(fraction)

    def _on_destroy(self, event):
        if event.widget is self:
            self._cancel()

    def _box(self, x, y, width, height, color, tag=None):
        self.create_rectangle(x, y, x + width, y + height, fill=color, width=0,
                              tags=(tag,) if tag else ())

    def _label(self, x, y, value, color, size=9):
        width = self.winfo_width()
        if 1 < width < self.WIDTH:
            size = max(7, round(size * width / self.WIDTH))
        self.create_text(x, y, text=value, font=('Segoe UI', size, 'bold'), fill=color)

    def _diamond(self, x, y, size, tag):
        x, y = round(x), round(y)
        tags = ('diamond', tag)
        self.create_polygon(x - size, y, x - size // 2, y - size // 2,
                            x + size // 2, y - size // 2, x + size, y,
                            x, y + size, fill='#52ded9', outline='#1d777d',
                            width=1, tags=tags)
        self.create_polygon(x - size + 1, y, x, y + size - 1, x, y + 1,
                            fill='#2db0b7', outline='')
        if size >= 6:
            self.create_line(x - size // 2 + 1, y - size // 2 + 1,
                             x + size // 2 - 1, y - size // 2 + 1, fill='#dbfff3')

    def _ground(self):
        self._box(0, 0, self.WIDTH, 104, '#d9edf0')
        for x, y in ((12, 13), (157, 8), (390, 10)):
            self._box(x, y + 4, 24, 6, '#eff9f7')
            self._box(x + 6, y, 13, 6, '#eff9f7')
        self.create_polygon(0, 70, 22, 70, 22, 62, 48, 62, 48, 76, 73, 76,
                            73, 90, 122, 90, 122, 84, 159, 84, 159, 102, 0, 102,
                            fill='#a9c8ad', outline='')
        self._box(0, 102, self.WIDTH, 30, '#a68564')
        self._box(0, 100, self.WIDTH, 6, '#729a62')
        self._box(0, 100, self.WIDTH, 2, '#8db176')
        for x, y in ((8, 115), (36, 124), (64, 113), (96, 120), (122, 113),
                     (145, 123), (173, 116), (230, 119), (310, 114), (370, 122), (396, 114)):
            self._box(x, y, 9, 3, '#b69370')
            self._box(x + 10, y + 3, 4, 3, '#896f53')

    def _armor(self, material, final):
        edge, shade, face, light = ARMOR_COLORS[material]
        for x, y, w, h, color in (
            # Leggings and boots.
            (53, 72, 13, 20, edge), (68, 72, 13, 20, edge),
            (55, 74, 10, 16, face), (69, 74, 10, 16, shade),
            (51, 88, 15, 12, edge), (68, 88, 15, 12, edge),
            (52, 90, 12, 6, shade), (70, 90, 12, 6, shade),
            (53, 90, 10, 2, light), (71, 90, 10, 2, light),
            # Chestplate, shoulders, helmet. Leave the eyes and mouth exposed.
            (48, 50, 10, 16, edge), (50, 51, 6, 12, face),
            (54, 48, 26, 26, edge), (57, 52, 20, 18, face),
            (59, 54, 16, 2, light), (57, 67, 20, 3, shade),
            (56, 24, 29, 9, edge), (58, 25, 25, 5, face),
            (60, 25, 18, 2, light), (56, 31, 6, 14, edge),
            (57, 32, 3, 10, shade), (80, 31, 5, 14, edge),
        ):
            self._box(x, y, w, h, color, 'armor')
        if final:
            for x, y, w, h, color in (
                (59, 29, 23, 2, '#c4a4eb'), (62, 57, 11, 2, '#c4a4eb'),
                (65, 59, 5, 5, '#a284d8'), (56, 94, 7, 2, '#c4a4eb'),
                (71, 94, 7, 2, '#c4a4eb'),
            ):
                self._box(x, y, w, h, color, 'armor-trim')

    def _miner(self, angle, equipment, fraction):
        for x, y, w, h, color in (
            (54, 72, 13, 23, '#4b59a0'), (67, 72, 13, 23, '#39467f'),
            (52, 93, 15, 7, '#404453'), (67, 93, 15, 7, '#404453'),
            (54, 48, 26, 26, '#309da4'), (54, 48, 26, 5, '#53b5b9'),
            (49, 49, 8, 13, '#338482'), (49, 62, 8, 13, '#aa7654'),
            (58, 24, 25, 24, '#bd8d64'), (58, 24, 25, 6, '#604536'),
            (58, 29, 4, 14, '#604536'), (81, 29, 3, 16, '#a37151'),
            (64, 34, 5, 4, '#f7f0dd'), (75, 34, 5, 4, '#f7f0dd'),
            (67, 35, 2, 3, '#475f9d'), (78, 35, 2, 3, '#475f9d'),
            (70, 39, 5, 3, '#9b6849'), (68, 42, 10, 3, '#674735'),
        ):
            self._box(x, y, w, h, color)

        armor = equipment['armor']
        if armor in ARMOR_COLORS:
            self._armor(armor, equipment['threshold'] == REBIRTH_POINTS)

        axis_x, axis_y = math.cos(angle), math.sin(angle)
        shoulder_x, shoulder_y = 78, 55

        def at(distance, offset=0):
            return (round(shoulder_x + axis_x * distance - axis_y * offset),
                    round(shoulder_y + axis_y * distance + axis_x * offset))

        sleeve = ARMOR_COLORS[armor][2] if armor in ARMOR_COLORS else '#329697'
        self.create_line(shoulder_x, shoulder_y, *at(9), width=10, fill=sleeve)
        self.create_line(*at(8), *at(22), width=8, fill='#bd8d64')
        self.create_line(*at(18, 2), *at(52, 2), width=5, fill='#604536')
        self.create_line(*at(21, 1), *at(49, 1), width=2, fill='#ad825a')
        head = [at(distance, offset) for distance, offset in
                ((50, -14), (56, -10), (58, -4), (58, 8), (54, 14),
                 (50, 14), (53, 7), (53, -4), (48, -9), (43, -10), (43, -14))]
        label, edge, material, shine = PICKAXES[equipment['pickaxe']]
        self.create_polygon(*[coordinate for point in head for coordinate in point],
                            fill=material, outline=edge, width=1, tags=('pickaxe-head',))
        self.create_line(*at(50, -11), *at(54, -8), *at(55, -3), fill=shine, width=2)
        if equipment['enchanted']:
            sweep = 0 if fraction is None else 4 * math.sin(2 * math.pi * fraction)
            self.create_line(*at(54, -5 + sweep), *at(55, 6 + sweep),
                             fill='#cea7f3', width=2, tags=('pickaxe-glint',))
        self._label(82, 119, label, '#2f3028')

    def _block(self, cracked):
        self._box(133, 70, 30, 30, '#566269', 'ore')
        self._box(135, 72, 26, 26, '#829095')
        self._box(135, 72, 26, 3, '#aebbb7')
        self._box(135, 75, 3, 20, '#a0aaa6')
        for x, y in ((139, 77), (151, 79), (141, 90), (154, 89)):
            self._box(x, y, 6, 4, '#247a82')
            self._box(x + 1, y, 4, 2, '#75e7dd')
        if cracked:
            self.create_line(149, 71, 146, 81, 151, 84, 147, 91, 150, 98,
                             fill='#3c494e', width=2, tags=('ore-cracks',))
            self.create_line(136, 85, 145, 85, 150, 84, 158, 78,
                             fill='#3c494e', width=2, tags=('ore-cracks',))

    @classmethod
    def _slot_bounds(cls, tasks):
        total_weight = sum(weight for _, weight, _ in tasks)
        offset = 0
        width = cls.SLOT_RIGHT - cls.SLOT_LEFT
        for _, weight, _ in tasks:
            left = cls.SLOT_LEFT + width * offset / total_weight
            offset += weight
            right = cls.SLOT_LEFT + width * offset / total_weight
            yield left, right

    def _cabinet(self):
        self._label(292, 28, 'Алмазы за сегодня', '#354a43', 11)
        self._box(self.CABINET_X, 44, self.CABINET_WIDTH, 56, '#604b38')
        self._box(self.CABINET_X + 2, 42, self.CABINET_WIDTH - 4, 6, '#aa8965')
        self._box(self.CABINET_X + 5, 48, self.CABINET_WIDTH - 10, 47, '#967653')
        self._box(self.CABINET_X + 4, 94, self.CABINET_WIDTH - 8, 4, '#b0916b')

        if not self._tasks:
            self._label(292, 71, 'Заданий пока нет', '#fcf4e5')
            return None

        destination = None
        for (task_id, _, completed), (left, right) in zip(self._tasks, self._slot_bounds(self._tasks)):
            center = (left + right) / 2
            size = min(10, int((right - left - 3) / 2))
            self._box(left, 52, right - left - 1, 39, '#4e4339', 'cabinet-slot')
            self._box(left + 1, 54, right - left - 3, 34, '#725d4c')
            if completed and task_id == self._animated_task:
                destination = (center, size)
            elif completed:
                self._diamond(center, self.SLOT_CENTER_Y, size, 'filled-diamond')

        done = sum(completed for _, _, completed in self._tasks)
        self._label(292, 119, f'Дел выполнено {done} из {len(self._tasks)}', '#2f3028')
        return destination

    def _debris(self, fraction):
        for vx, vy, color in ((-25, -30, '#acc5c4'), (20, -39, '#85e8de'),
                              (36, -26, '#acbec2'), (-12, -42, '#d7f5e4'),
                              (29, -15, '#53c8c5'), (-33, -16, '#707c85')):
            x = 148 + vx * fraction
            y = 84 + vy * fraction + 39 * fraction * fraction
            self._box(round(x), round(y), 4, 4, color)

    def _angle(self, fraction):
        if fraction is None:
            return -0.57
        if fraction < self.FIRST_HIT:
            return -1.30 + 1.80 * fraction / self.FIRST_HIT
        if fraction < self.IMPACT:
            # Pull back briefly before the second strike.
            pullback = (fraction - self.FIRST_HIT) / (self.IMPACT - self.FIRST_HIT)
            return 0.50 - 1.40 * math.sin(math.pi * pullback)
        return 0.50 - 1.07 * (fraction - self.IMPACT) / (1 - self.IMPACT)

    def _draw(self, fraction=None):
        self.delete('all')
        self._ground()
        equipment, _ = equipment_for_points(self._points)
        self._miner(self._angle(fraction), equipment, fraction)
        impact = fraction is not None and fraction >= self.IMPACT
        if not impact and (fraction is not None or any(not done for _, _, done in self._tasks)):
            self._block(fraction is not None and fraction >= self.FIRST_HIT)
        destination = self._cabinet()
        if impact and destination is not None:
            center, size = destination
            travel = (fraction - self.IMPACT) / (1 - self.IMPACT)
            if travel < 0.72:
                self._debris(travel)
            x = 148 + (center - 148) * travel
            y = 83 + (self.SLOT_CENTER_Y - 83) * travel - 34 * math.sin(math.pi * travel)
            self._diamond(x, y, round(10 + (size - 10) * travel), 'flying-diamond')

        width = self.winfo_width()
        if width > 1:
            scale = min(1, width / self.WIDTH)
            if scale < 1:
                self.scale('all', 0, 0, scale, scale)
            self.move('all', (width - self.WIDTH * scale) / 2, 0)
