"""Meridian.

One outfield player meets the ball on its future path. A keeper lives on the
shot line. Everyone else is either the last defender or a runner. Kicks are
aimed as a change in ball velocity, not as a direction from the boot.
"""

from __future__ import annotations

import math

from soccer import PlayerAction, TeamAction, TeamController

HORIZON = 45
STRIDE = 2
STEPS = tuple(range(0, HORIZON, STRIDE))
WALL = 0.75


def _clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _arrival(px, py, vx, vy, tx, ty, reach2, reach, vmax, acc, hz):
    """Ticks until a player can get within kick range of a point."""
    dx = tx - px
    dy = ty - py
    d2 = dx * dx + dy * dy
    if d2 <= reach2:
        return 0.0
    dist = math.sqrt(d2)
    gap = dist - reach
    inv = 1.0 / dist
    closing = (vx * dx + vy * dy) * inv
    if closing < 0.0:
        gap += (closing * closing) / (acc + acc)
        closing = 0.0
    if closing >= vmax:
        return gap / closing * hz
    t_acc = (vmax - closing) / acc
    d_acc = closing * t_acc + 0.5 * acc * t_acc * t_acc
    if d_acc >= gap:
        disc = closing * closing + 2.0 * acc * gap
        return ((-closing + math.sqrt(disc)) / acc) * hz
    return (t_acc + (gap - d_acc) / vmax) * hz


def _send(bvx, bvy, ux, uy, speed, cap):
    """Kick that leaves the ball travelling along (ux, uy) at up to `speed`.

    The engine adds kick_direction * (power * kick_impulse) to the current
    ball velocity and then caps the result at 30. We pick an outgoing
    velocity that is actually reachable, then aim at the difference.
    """
    if speed > 30.0:
        speed = 30.0
    if speed < 0.5:
        speed = 0.5
    dot = ux * bvx + uy * bvy
    v2 = bvx * bvx + bvy * bvy
    ix = ux * speed - bvx
    iy = uy * speed - bvy
    mag2 = ix * ix + iy * iy
    cap2 = cap * cap
    if mag2 > cap2:
        disc = dot * dot - v2 + cap2
        if disc < 0.0:
            return None
        speed = dot + math.sqrt(disc)
        if speed < 3.0:
            return None
        if speed > 30.0:
            speed = 30.0
        ix = ux * speed - bvx
        iy = uy * speed - bvy
        mag2 = ix * ix + iy * iy
    mag = math.sqrt(mag2) if mag2 > 0.0 else 0.0
    if mag < 1e-8:
        return ux, uy, 0.0
    power = mag / cap
    if power > 1.0:
        power = 1.0
    inv = 1.0 / mag
    return ix * inv, iy * inv, power


def _seg_d2(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    ls = dx * dx + dy * dy
    if ls < 1e-12:
        ex = px - ax
        ey = py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / ls
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    ex = px - (ax + dx * t)
    ey = py - (ay + dy * t)
    return ex * ex + ey * ey


def _unit(dx, dy):
    d = math.hypot(dx, dy)
    if d < 1e-9 or d != d:
        return 1.0, 0.0
    return dx / d, dy / d


def _toward(px, py, tx, ty, ease):
    dx = tx - px
    dy = ty - py
    d2 = dx * dx + dy * dy
    if d2 < 0.09:
        return 0.0, 0.0
    d = math.sqrt(d2)
    if ease and d < 2.2:
        scale = d / 2.2
        return dx / d * scale, dy / d * scale
    return dx / d, dy / d


def _outside(x, y, rad):
    d2 = x * x + y * y
    r2 = rad * rad
    if d2 >= r2:
        return x, y
    if d2 < 1e-12:
        return -rad, 0.0
    d = math.sqrt(d2)
    s = rad / d
    return x * s, y * s


def _flight(dist, speed, dt, fr):
    """Ticks for a ball already moving at `speed` to cover `dist` under friction."""
    if dist <= 0.4:
        return 1.0
    if speed < 0.8 or fr <= 0.0 or fr >= 1.0:
        return 999.0
    reach = speed * dt / (1.0 - fr)
    if reach < 1e-6 or dist >= reach * 0.97:
        return 999.0
    ratio = 1.0 - dist / reach
    if ratio <= 0.002 or ratio >= 1.0:
        return 999.0
    ticks = math.log(ratio) / math.log(fr)
    if ticks != ticks or ticks < 0.0:
        return 999.0
    return ticks


def _keep_out(px, py, mx, my, rad):
    """Drop any inward component while we are standing near the centre circle."""
    d2 = px * px + py * py
    outer = rad + 4.0
    if d2 >= outer * outer or d2 < 1e-8:
        return mx, my
    d = math.sqrt(d2)
    ux = px / d
    uy = py / d
    inward = mx * ux + my * uy
    if inward < 0.0:
        mx -= inward * ux
        my -= inward * uy
    if d < rad + 0.8:
        mx += ux
        my += uy
    mag = math.hypot(mx, my)
    if mag > 1.0:
        inv = 1.0 / mag
        return mx * inv, my * inv
    if mag < 1e-9:
        return ux, uy
    return mx, my


def _sanitize(mv):
    mx, my = mv
    if mx != mx or my != my:
        return 0.0, 0.0
    mag2 = mx * mx + my * my
    if mag2 != mag2:
        return 0.0, 0.0
    if mag2 > 1.0:
        inv = 0.999 / math.sqrt(mag2)
        return mx * inv, my * inv
    return mx, my


def _aim(dx, dy, pw):
    """Unit kick direction at length 0.999 and power inside [0, 1]."""
    if dx != dx or dy != dy or pw != pw:
        return 1.0, 0.0, 0.0
    m2 = dx * dx + dy * dy
    if m2 != m2 or m2 < 1e-12:
        dx, dy = 0.999, 0.0
    else:
        inv = 0.999 / math.sqrt(m2)
        dx *= inv
        dy *= inv
    if pw < 0.0:
        pw = 0.0
    elif pw > 1.0:
        pw = 1.0
    return dx, dy, pw


class MyTeam(TeamController):
    name = "meridian"
    version = "4"

    def __init__(self):
        self.ready = False
        self.keep = 4
        self.lock_id = -1
        self.lock_until = -1
        self.lock_x = 0.0
        self.lock_y = 0.0
        self.hx = [0.0] * HORIZON
        self.hy = [0.0] * HORIZON
        self.mx = [0.0] * 5
        self.my = [0.0] * 5
        self.mvx = [0.0] * 5
        self.mvy = [0.0] * 5
        self.ox = [0.0] * 5
        self.oy = [0.0] * 5
        self.ovx = [0.0] * 5
        self.ovy = [0.0] * 5
        self.meta = [0.0] * 5
        self.midx = [0] * 5
        self.shot_y = (0.0,) * 7
        self.sx = [0.0] * 5
        self.sy = [0.0] * 5
        self.b0 = 0.0
        self.skip_gap = 4.0
        self.lim = [0.0] * HORIZON

    def reset(self, seed):
        self.keep = 4
        self.lock_id = -1
        self.lock_until = -1

    def initial_formation(self, field):
        # 1.6 keeps a player outside the circle after ±1.5 kickoff jitter.
        edge = -(field.centre_circle_radius + 1.6)
        gx = field.my_goal[0] + field.player_radius + 1.6
        return [
            (gx, 0.0),
            (-32.0, 0.0),
            (edge, -16.5),
            (edge, 16.5),
            (edge, 0.0),
        ]

    def _boot(self, field):
        self.dt = 1.0 / field.simulation_hz
        self.hz = field.simulation_hz
        self.fr = field.ball_friction
        self.vmax = field.max_speed
        self.acc = field.acceleration if field.acceleration > 1.0 else 20.0
        self.cap = field.kick_impulse
        self.reach = field.kick_range - 0.12
        if self.reach < 1.4:
            self.reach = 1.4
        self.reach2 = self.reach * self.reach
        lims = [0.0] * HORIZON
        lim2 = [0.0] * HORIZON
        for t in range(HORIZON):
            lim = self.reach + self.vmax * (t * self.dt)
            lims[t] = lim
            lim2[t] = lim * lim
        self.lim = lims
        self.lim2 = lim2
        self.b0 = 0.0
        self.skip_gap = (self.vmax + 2.0) * (8.0 / self.hz)
        # drift[k]: furthest a ball moving at unit speed can get in k ticks.
        # Walls and the goal mouth only ever shorten that.
        drift = [0.0] * (HORIZON + 2)
        step = self.dt
        for k in range(1, HORIZON + 2):
            drift[k] = drift[k - 1] + step
            step *= self.fr
        self.drift = [d * 1.000001 + 1e-6 for d in drift]
        self.xlim = field.width * 0.5 - field.ball_radius
        self.ylim = field.height * 0.5 - field.ball_radius
        self.mouth = field.goal_width * 0.5
        self.post = self.mouth - 0.65
        self.ccr = field.centre_circle_radius
        self.pr = field.player_radius
        self.safe = self.ccr + self.pr + 1.5
        self.gx = field.my_goal[0]
        self.ogx = field.opponent_goal[0]
        half = self.post
        self.shot_y = (
            -half,
            -half * 0.7,
            -half * 0.35,
            0.0,
            half * 0.35,
            half * 0.7,
            half,
        )
        self.ready = True

    def _load(self, players, xs, ys, vxs, vys):
        n = len(players)
        if n > 5:
            n = 5
        for i in range(n):
            p = players[i]
            xs[i], ys[i] = p.position
            vxs[i], vys[i] = p.velocity
        return n

    def _roll(self, bx, by, bvx, bvy):
        dt = self.dt
        fr = self.fr
        xlim = self.xlim
        ylim = self.ylim
        mouth = self.mouth
        hx = self.hx
        hy = self.hy
        spread = math.sqrt(bvx * bvx + bvy * bvy) * self.drift[HORIZON]
        if -xlim < bx - spread and bx + spread < xlim and -ylim < by - spread and by + spread < ylim:
            for i in range(HORIZON):
                bx += bvx * dt
                by += bvy * dt
                bvx *= fr
                bvy *= fr
                hx[i] = bx
                hy[i] = by
            return
        for i in range(HORIZON):
            bx += bvx * dt
            by += bvy * dt
            if by > ylim:
                by = ylim - (by - ylim)
                bvy = -bvy * WALL
            elif by < -ylim:
                by = -ylim - (by + ylim)
                bvy = -bvy * WALL
            if bx > xlim:
                if by > mouth or by < -mouth:
                    bx = xlim - (bx - xlim)
                    bvx = -bvx * WALL
                else:
                    bx = xlim
                    bvx *= 0.15
                    bvy *= 0.15
            elif bx < -xlim:
                if by > mouth or by < -mouth:
                    bx = -xlim - (bx + xlim)
                    bvx = -bvx * WALL
                else:
                    bx = -xlim
                    bvx *= 0.15
                    bvy *= 0.15
            bvx *= fr
            bvy *= fr
            hx[i] = bx
            hy[i] = by

    def _meet(self, px, py, vx, vy):
        """Earliest path index the player can reach, and the arrival tick.

        Samples that a full-speed player cannot reach yet are skipped. The
        skip is a lower bound, so the first feasible stride point is unchanged.
        """
        hx = self.hx
        hy = self.hy
        lim = self.lim
        lim2 = self.lim2
        reach = self.reach
        reach2 = self.reach2
        vmax = self.vmax
        acc = self.acc
        hz = self.hz
        skip_gap = self.skip_gap
        best_i = 0
        best_d = 1e18
        t = 0
        while t < HORIZON:
            x = hx[t]
            y = hy[t]
            dx = x - px
            dy = y - py
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best_i = t
            if d2 <= lim2[t]:
                if _arrival(px, py, vx, vy, x, y, reach2, reach, vmax, acc, hz) <= t + 0.45:
                    best = t
                    lo = t - STRIDE + 1
                    if lo < 0:
                        lo = 0
                    k = t - 1
                    while k >= lo:
                        if _arrival(px, py, vx, vy, hx[k], hy[k], reach2, reach, vmax, acc, hz) <= k + 0.45:
                            best = k
                        else:
                            break
                        k -= 1
                    return best, float(best)
                t += STRIDE
            else:
                edge = lim[t] + skip_gap
                if d2 > edge * edge:
                    t += 8
                else:
                    t += STRIDE
        eta = _arrival(px, py, vx, vy, hx[best_i], hy[best_i], reach2, reach, vmax, acc, hz)
        if eta < float(best_i):
            eta = float(best_i)
        return best_i, eta

    def _shot_cross(self, bx, by, bvx, bvy):
        """Y where a goal-bound ball crosses the keeper's line. None if it misses.

        The rolled path is checked first so a sideline rebound into the mouth
        is seen. A straight extrapolation is used only when that path cannot
        hit a wall before the line.
        """
        line = self.gx + 1.35
        if bvx >= -1.5 or bx <= line:
            return None
        mouth = self.mouth
        lim = self.ylim
        hx = self.hx
        hy = self.hy
        prev_x = bx
        prev_y = by
        i = 0
        while i < HORIZON:
            x = hx[i]
            y = hy[i]
            if x <= line and prev_x > line:
                den = x - prev_x
                if den > -1e-6 and den < 1e-6:
                    cy = y
                else:
                    cy = prev_y + (line - prev_x) / den * (y - prev_y)
                if cy <= mouth + 1.6 and cy >= -(mouth + 1.6):
                    return cy
                return None
            prev_x = x
            prev_y = y
            i += 1
        if bvy > 1e-6 or bvy < -1e-6:
            for wall in (lim, -lim):
                if (wall - by) * bvy > 0.0:
                    xhit = bx + (wall - by) * bvx / bvy
                    if xhit < bx and xhit > line:
                        return None
        cy = by + bvy * ((line - bx) / bvx)
        if cy != cy or cy > mouth + 1.5 or cy < -(mouth + 1.5):
            return None
        if cy > lim or cy < -lim or by > lim or by < -lim:
            return None
        return cy

    def _gk_spot(self, bx, by, bvx, bvy, cross):
        gx = self.gx
        gkx = self.mx[0]
        post = self.mouth - 0.45
        # A ball already behind the keeper stays there. Chasing it through
        # the six-yard box is how own goals happen.
        past = bx < gkx - 0.15 and bx < -46.2 and abs(by) < self.mouth + 1.2
        if cross is not None:
            y = _clamp(cross, -post, post)
            x = gkx if past else gx + 1.25
            return x, y
        if bx < -25.0:
            cover = False
            i = 1
            while i < 5:
                if self.mx[i] < bx - 1.0:
                    cover = True
                    break
                i += 1
            if not cover:
                span = bx - gx
                if span < 2.0:
                    span = 2.0
                out = -45.0
                front = bx - 1.4
                if out > front:
                    out = front
                if out < gx + 1.2:
                    out = gx + 1.2
                frac = (out - gx) / span
                if frac < 0.0:
                    frac = 0.0
                elif frac > 0.85:
                    frac = 0.85
                y = _clamp(by * frac, -post, post)
                if past:
                    out = gkx
                return out, y
            if bx < -38.0:
                y = _clamp(by * 0.55 + bvy * 0.15, -post, post)
                return (gkx if past else gx + 1.7), y
            y = _clamp(by * 0.35, -post * 0.85, post * 0.85)
            return -46.5, y
        span = bx - gx
        if span < 16.0:
            span = 16.0
        shade = by * (4.2 / span)
        return -47.4, _clamp(shade, -4.2, 4.2)

    def act(self, obs):
        if not self.ready:
            self._boot(obs.field)
        ball = obs.ball
        bx, by = ball.position
        bvx, bvy = ball.velocity
        spd2 = bvx * bvx + bvy * bvy
        centre2 = bx * bx + by * by
        ctrl = ball.controlling_team
        their_ko = ctrl == 1 and centre2 < 4.0 and spd2 < 0.25
        our_ko = ctrl == 0 and centre2 < 4.0 and spd2 < 0.25
        if their_ko:
            self.lock_id = -1
            return self._ring(obs)

        mx = self.mx
        my = self.my
        mvx = self.mvx
        mvy = self.mvy
        ox = self.ox
        oy = self.oy
        ovx = self.ovx
        ovy = self.ovy
        meta = self.meta
        midx = self.midx
        meet = self._meet
        them = obs.opponents
        n = self._load(obs.my_players, mx, my, mvx, mvy)
        on = self._load(them, ox, oy, ovx, ovy)
        self._roll(bx, by, bvx, bvy)
        self.b0 = math.sqrt(spd2) if spd2 > 1e-8 else 0.0
        self.skip_gap = (self.vmax + self.b0 + 2.0) * (8.0 / self.hz)

        for i in range(1, n):
            midx[i], meta[i] = meet(mx[i], my[i], mvx[i], mvy[i])

        hunter = 1
        best = meta[1]
        i = 2
        while i < n:
            eta = meta[i]
            if eta < best - 0.35 or (eta <= best + 0.35 and mx[i] > mx[hunter]):
                best = eta
                hunter = i
            i += 1
        deep = self._deepest(-1)
        if hunter == deep:
            second = self._second(deep)
            if second >= 0 and meta[second] <= meta[deep] + 2.5:
                hunter = second
                best = meta[hunter]
        kept = self.keep
        if kept != hunter and 0 < kept < n and meta[kept] <= best + 2.0:
            if kept != deep or meta[kept] <= meta[hunter] + 0.4:
                hunter = kept
                best = meta[hunter]
        self.keep = hunter
        threat = self._front_opponent()

        tick = obs.tick
        locked = self.lock_id
        if locked == hunter or tick > self.lock_until:
            locked = -1
        anchor = self._deepest(hunter if locked < 0 else -2)
        if locked >= 0 and anchor == locked:
            alt = self._deepest_skip(hunter, locked)
            if alt >= 0:
                anchor = alt
        if anchor < 0:
            anchor = self._deepest(hunter)

        my_eta = meta[hunter]
        gk_eta = 1e9
        gk_idx = 0
        if bx < -18.0 or bvx < -1.0:
            gk_idx, gk_eta = meet(mx[0], my[0], mvx[0], mvy[0])
        cross = self._shot_cross(bx, by, bvx, bvy)

        # opp_best is only compared against my_eta + 1.6 and gk_eta + 1.0
        # (`sweep`, `attacking`). `cap` sits a tick above both, so an opponent
        # that provably cannot arrive before `cap` changes neither and its
        # search is skipped. Any new use of opp_best has to raise `cap` to match.
        cap = my_eta + 2.6
        if cross is None and bx < -34.0 and gk_eta + 2.0 > cap:
            cap = gk_eta + 2.0
        k = int(cap) + 3 if cap < HORIZON - 2 else HORIZON + 1
        if k < 0:
            k = 0
        elif k > HORIZON + 1:
            k = HORIZON + 1
        travel = self.b0 * self.drift[k]
        reach = self.reach
        vmax = self.vmax
        hz = self.hz
        skip_lim = travel + reach + (vmax + 0.8) * (cap + 1.0) / hz
        skip2 = skip_lim * skip_lim
        opp_best = 1e9
        j = 0
        while j < on:
            dx = ox[j] - bx
            dy = oy[j] - by
            if dx * dx + dy * dy <= skip2:
                eta = meet(ox[j], oy[j], ovx[j], ovy[j])[1]
                if eta < opp_best:
                    opp_best = eta
            j += 1

        sweep = (
            cross is None
            and bx < -34.0
            and gk_eta + 1.0 < opp_best
            and gk_eta + 0.5 < my_eta
        )
        attacking = our_ko or (ctrl == 0 and opp_best + 1.0 >= my_eta) or (opp_best > my_eta + 1.6)

        kicker = self._kicker(obs, hunter, bx)
        kick = None
        recv = -1
        dest_x = bx
        dest_y = by
        if kicker == 0:
            kick = self._gk_kick(obs, bx, by, bvx, bvy)
        elif kicker > 0 and our_ko:
            kick = self._kickoff_kick(obs, kicker, bx, by, bvx, bvy)
        elif kicker > 0:
            kick = self._strike(obs, kicker, bx, by, bvx, bvy, attacking)
        if kick is not None:
            recv = kick[5]
            dest_x = kick[3]
            dest_y = kick[4]
            if recv > 0 and recv != anchor:
                self.lock_id = recv
                self.lock_until = tick + 26
                self.lock_x = dest_x
                self.lock_y = dest_y
                locked = recv

        spots_x = self.sx
        spots_y = self.sy
        spots_x[0], spots_y[0] = self._gk_spot(bx, by, bvx, bvy, cross)
        if sweep:
            sx = self.hx[gk_idx]
            sy = self.hy[gk_idx]
            spots_x[0] = _clamp(sx, self.gx + 0.8, -36.0)
            spots_y[0] = _clamp(sy, -8.0, 8.0)

        free1 = -1
        free2 = -1
        i = 1
        while i < n:
            if i != hunter and i != anchor:
                if free1 < 0:
                    free1 = i
                else:
                    free2 = i
            i += 1

        if attacking or our_ko:
            self._attack_spots(bx, by, free1, free2, spots_x, spots_y, our_ko)
        else:
            self._defend_spots(bx, by, free1, free2, threat, spots_x, spots_y)

        spots_x[anchor], spots_y[anchor] = self._anchor_spot(threat, bx, by, attacking)
        if hunter != kicker:
            idx = midx[hunter]
            if idx < 2 and spd2 > 1.0:
                idx = 3 if HORIZON > 3 else idx
            spots_x[hunter] = self.hx[idx]
            spots_y[hunter] = self.hy[idx]
        if sweep and hunter != kicker:
            # Keeper has the ball won. Hold a covering slot instead of diving in.
            spots_x[hunter] = _clamp(bx + 6.0, -42.0, -28.0)
            spots_y[hunter] = _clamp(by, -10.0, 10.0)
        elif (not attacking) and bx < 0.0 and hunter != kicker and spd2 < 49.0:
            held2 = 1e18
            j = 0
            while j < on:
                dx = ox[j] - bx
                dy = oy[j] - by
                d2 = dx * dx + dy * dy
                if d2 < held2:
                    held2 = d2
                j += 1
            if held2 < 10.24:
                dx = self.gx - bx
                dy = -by
                d2 = dx * dx + dy * dy
                if d2 > 0.25:
                    inv = 1.0 / math.sqrt(d2)
                    ux = dx * inv
                    uy = dy * inv
                else:
                    ux, uy = -1.0, 0.0
                lo = self.gx + 1.5
                hi = bx - 0.3
                if hi < lo:
                    hi = lo
                spots_x[hunter] = _clamp(bx + ux * 2.4, lo, hi)
                spots_y[hunter] = _clamp(by + uy * 2.4, -18.0, 18.0)
                press = free1
                if free2 >= 0 and (press < 0 or meta[free2] < meta[press]):
                    press = free2
                if press >= 0 and press != locked:
                    side = 1.0 if spots_y[anchor] < by else -1.0
                    plo = self.gx + 2.0
                    phi = bx - 0.2
                    if phi < plo:
                        phi = plo
                    spots_x[press] = _clamp(bx + ux * 3.8 - uy * 3.3 * side, plo, phi)
                    spots_y[press] = _clamp(by + uy * 3.8 + ux * 3.3 * side, -22.0, 22.0)
        if locked > 0 and locked != kicker and locked != hunter:
            spots_x[locked] = self.lock_x
            spots_y[locked] = self.lock_y

        actions = TeamAction()
        out = actions.players
        if n > 0:
            if kick is not None and kicker == 0:
                kd0, kd1, kp = _aim(kick[0], kick[1], kick[2])
                out[0] = PlayerAction(_toward(mx[0], my[0], bx, by, False), (kd0, kd1), kp)
            else:
                ease = cross is None and bx > -32.0
                out[0] = PlayerAction(_sanitize(_toward(mx[0], my[0], spots_x[0], spots_y[0], ease)))
        for i in range(1, n):
            if kick is not None and i == kicker:
                if kick[6] == 1:
                    mv = self._dribble_move(i, kick[3], kick[4])
                else:
                    mv = _toward(mx[i], my[i], bx, by, False)
                kd0, kd1, kp = _aim(kick[0], kick[1], kick[2])
                out[i] = PlayerAction(_sanitize(mv), (kd0, kd1), kp)
            else:
                chase = i == hunter and i != kicker
                out[i] = PlayerAction(_sanitize(_toward(mx[i], my[i], spots_x[i], spots_y[i], not chase)))
        return actions

    def _ring(self, obs):
        """Opponent kickoff: nobody crosses the centre circle."""
        actions = TeamAction()
        safe = self.safe
        n = len(obs.my_players)
        spots = (
            (self.gx + 1.8, 0.0),
            (-28.0, 0.0),
            (-15.5, -14.0),
            (-15.5, 14.0),
            (-safe - 0.3, 0.0),
        )
        i = 0
        while i < n:
            p = obs.my_players[i]
            px, py = p.position
            tx, ty = spots[i] if i < 5 else (-20.0, 0.0)
            tx, ty = _outside(tx, ty, safe)
            if tx > -1.0:
                tx = -safe
            mx, my = _toward(px, py, tx, ty, True)
            mx, my = _keep_out(px, py, mx, my, safe)
            actions.move(p.id, _sanitize((mx, my)))
            i += 1
        return actions

    def _deepest(self, skip):
        mx = self.mx
        best = -1
        best_x = 1e9
        i = 1
        while i < 5:
            if i != skip and mx[i] < best_x:
                best_x = mx[i]
                best = i
            i += 1
        return best

    def _deepest_skip(self, a, b):
        mx = self.mx
        best = -1
        best_x = 1e9
        i = 1
        while i < 5:
            if i != a and i != b and mx[i] < best_x:
                best_x = mx[i]
                best = i
            i += 1
        return best

    def _front_opponent(self):
        """Opponent furthest into our half — the one the anchor has to stay behind."""
        best = 0
        best_x = self.ox[0]
        j = 1
        while j < 5:
            if self.ox[j] < best_x:
                best_x = self.ox[j]
                best = j
            j += 1
        return best

    def _second(self, skip):
        meta = self.meta
        best = -1
        best_e = 1e9
        i = 1
        while i < 5:
            if i != skip and meta[i] < best_e:
                best_e = meta[i]
                best = i
            i += 1
        return best

    def _kicker(self, obs, hunter, bx):
        if bx < -32.0 and obs.can_kick(0):
            return 0
        if 0 < hunter < 5 and obs.can_kick(hunter):
            return hunter
        best = -1
        best_x = -1e9
        i = 1
        while i < 5:
            if obs.can_kick(i) and self.mx[i] > best_x:
                best_x = self.mx[i]
                best = i
            i += 1
        if best >= 0:
            return best
        if obs.can_kick(0):
            return 0
        return -1

    def _attack_spots(self, bx, by, a, b, sx, sy, kickoff):
        if a < 0:
            return
        if kickoff:
            ya = 22.0 if self.my[a] >= 0.0 else -22.0
            sx[a] = 34.0
            sy[a] = ya
            if b >= 0:
                sx[b] = 30.0
                sy[b] = -ya
            return
        if bx > 16.0:
            rx = 44.0
            ry = -6.2 if by >= 0.0 else 6.2
        elif bx > -8.0:
            rx = _clamp(bx + 16.0, 8.0, 42.0)
            ry = -18.0 if by >= 0.0 else 18.0
        else:
            rx = _clamp(bx + 20.0, 2.0, 24.0)
            ry = -18.0 if by >= 0.0 else 18.0
        deep_x = self.ox[0]
        j = 1
        while j < 5:
            if self.ox[j] < deep_x:
                deep_x = self.ox[j]
            j += 1
        # Cherry-picker already in our half: hold one teammate goalside of the ball.
        if deep_x < 0.0:
            qx = _clamp(bx - 6.5, -34.0, 40.0)
            qy = _clamp(by * -0.2, -11.0, 11.0)
            if b < 0:
                sx[a] = rx
                sy[a] = ry
                return
            if self.mx[a] >= self.mx[b]:
                sx[a], sy[a] = rx, ry
                sx[b], sy[b] = qx, qy
            else:
                sx[b], sy[b] = rx, ry
                sx[a], sy[a] = qx, qy
            return
        qx = _clamp(bx + 4.0, -12.0, 28.0)
        qy = 13.5 if by < 0.0 else -13.5
        if b < 0:
            sx[a] = rx
            sy[a] = ry
            return
        d_keep = (self.mx[a] - rx) ** 2 + (self.my[a] - ry) ** 2 + (self.mx[b] - qx) ** 2 + (self.my[b] - qy) ** 2
        d_swap = (self.mx[a] - qx) ** 2 + (self.my[a] - qy) ** 2 + (self.mx[b] - rx) ** 2 + (self.my[b] - ry) ** 2
        if d_swap < d_keep:
            sx[a], sy[a] = qx, qy
            sx[b], sy[b] = rx, ry
        else:
            sx[a], sy[a] = rx, ry
            sx[b], sy[b] = qx, qy

    def _defend_spots(self, bx, by, a, b, threat, sx, sy):
        t0, t1 = self._threats(threat)
        if a >= 0 and b >= 0 and t0 >= 0 and t1 >= 0:
            s0x, s0y = self._mark(t0, bx, by)
            s1x, s1y = self._mark(t1, bx, by)
            mx = self.mx
            my = self.my
            keep = (mx[a] - s0x) ** 2 + (my[a] - s0y) ** 2
            keep += (mx[b] - s1x) ** 2 + (my[b] - s1y) ** 2
            swap = (mx[a] - s1x) ** 2 + (my[a] - s1y) ** 2
            swap += (mx[b] - s0x) ** 2 + (my[b] - s0y) ** 2
            if swap < keep:
                sx[a], sy[a] = s1x, s1y
                sx[b], sy[b] = s0x, s0y
            else:
                sx[a], sy[a] = s0x, s0y
                sx[b], sy[b] = s1x, s1y
            return
        ids = (a, b)
        threats = (t0, t1)
        k = 0
        while k < 2:
            pid = ids[k]
            if pid < 0:
                k += 1
                continue
            th = threats[k]
            if th < 0 or self.ox[th] > 24.0 and bx < 8.0:
                line = _clamp(bx - 11.0, -40.0, -12.0)
                sx[pid] = line
                sy[pid] = _clamp(by + (-8.0 if k == 0 else 8.0), -14.0, 14.0)
            else:
                sx[pid], sy[pid] = self._mark(th, bx, by)
            k += 1

    def _threats(self, skip_opp):
        ox = self.ox
        oy = self.oy
        gx = self.gx
        best0 = 1e18
        best1 = 1e18
        i0 = -1
        i1 = -1
        j = 0
        while j < 5:
            if j != skip_opp:
                dx = ox[j] - gx
                dy = oy[j]
                s = dx * dx + dy * dy
                if s < best0:
                    best1 = best0
                    i1 = i0
                    best0 = s
                    i0 = j
                elif s < best1:
                    best1 = s
                    i1 = j
            j += 1
        return i0, i1

    def _mark(self, j, bx, by):
        ox = self.ox[j] + self.ovx[j] * 0.28
        oy = self.oy[j] + self.ovy[j] * 0.28
        dx = self.gx - ox
        dy = -oy
        dist = math.hypot(dx, dy)
        if dist < 0.4:
            ux, uy = -1.0, 0.0
        else:
            ux = dx / dist
            uy = dy / dist
        sx = ox + ux * 3.0
        sy = oy + uy * 3.0
        bdx = bx - ox
        bdy = by - oy
        bd = math.hypot(bdx, bdy)
        if bd > 0.4:
            sx += bdx / bd * 1.15
            sy += bdy / bd * 1.15
        if sx > ox - 0.9:
            sx = ox - 0.9
        return _clamp(sx, -48.0, 46.0), _clamp(sy, -28.0, 28.0)

    def _anchor_spot(self, threat, bx, by, attacking):
        # In our box the keeper takes the ball side. The anchor takes the far post.
        if not attacking and bx < -32.0:
            far = -5.7 if by >= 0.0 else 5.7
            return -47.5, far
        if threat < 0:
            return -28.0, _clamp(by * 0.2, -6.0, 6.0)
        tx = self.ox[threat] + self.ovx[threat] * 0.3
        ty = self.oy[threat] + self.ovy[threat] * 0.3
        sx = tx - 4.2
        sy = ty * 0.42 + by * 0.1
        if sx > tx - 1.6:
            sx = tx - 1.6
        if attacking:
            if tx > -6.0:
                sx = _clamp(sx, -35.0, -10.0)
            else:
                # Stay goalside of a striker already camped in our half.
                cap = tx - 3.5
                if cap > -12.0:
                    cap = -12.0
                if cap < -44.0:
                    cap = -44.0
                if sx > cap:
                    sx = cap
                sx = _clamp(sx, -44.0, cap)
        else:
            sx = _clamp(min(sx, bx - 3.0), -46.0, -8.0)
        return sx, _clamp(sy, -11.0, 11.0)

    def _dribble_move(self, pid, tx, ty):
        px = self.mx[pid]
        py = self.my[pid]
        dx = tx - px
        dy = ty - py
        mag = math.hypot(dx, dy)
        if mag < 1e-6:
            ux, uy = 1.0, 0.0
        else:
            ux = dx / mag
            uy = dy / mag
        j = 0
        while j < 5:
            ex = px - self.ox[j]
            ey = py - self.oy[j]
            d2 = ex * ex + ey * ey
            if 0.05 < d2 < 30.0:
                inv = 1.8 / d2
                ux += ex * inv
                uy += ey * inv
            j += 1
        j = 0
        while j < 5:
            if j != pid:
                ex = px - self.mx[j]
                ey = py - self.my[j]
                d2 = ex * ex + ey * ey
                if 0.05 < d2 < 10.0:
                    inv = 0.7 / d2
                    ux += ex * inv
                    uy += ey * inv
            j += 1
        if px < -36.0 and ux < 0.15:
            ux = 0.15
        return _unit(ux, uy)

    def _lane(self, ax, ay, bx, by):
        worst = 1e18
        j = 0
        while j < 5:
            ox = self.ox[j] + self.ovx[j] * 0.3
            oy = self.oy[j] + self.ovy[j] * 0.3
            d2 = _seg_d2(ox, oy, ax, ay, bx, by)
            if d2 < worst:
                worst = d2
            j += 1
        return worst

    def _point_eta(self, tx, ty, skip):
        reach2 = self.reach2
        reach = self.reach
        vmax = self.vmax
        acc = self.acc
        hz = self.hz
        scale = vmax / hz
        ox = self.ox
        oy = self.oy
        ovx = self.ovx
        ovy = self.ovy
        best = 1e9
        j = 0
        while j < 5:
            if j != skip:
                dx = ox[j] - tx
                dy = oy[j] - ty
                d2 = dx * dx + dy * dy
                bound = best * scale + reach
                if d2 <= bound * bound:
                    eta = _arrival(ox[j], oy[j], ovx[j], ovy[j], tx, ty, reach2, reach, vmax, acc, hz)
                    if eta < best:
                        best = eta
            j += 1
        return best

    def _lead(self, bx, by, bvx, bvy, mate, aimx, aimy):
        """Two-step lead into the run, not just onto the receiver's feet."""
        mx = self.mx[mate]
        my = self.my[mate]
        mvx = self.mvx[mate]
        mvy = self.mvy[mate]
        dx = aimx - mx
        dy = aimy - my
        dist = math.hypot(dx, dy)
        if dist < 0.8:
            rvx, rvy = self.vmax, 0.0
        else:
            rvx = dx / dist * self.vmax
            rvy = dy / dist * self.vmax
        if mvx > 0.7:
            rvx = rvx * 0.5 + mvx * 0.5
            rvy = rvy * 0.5 + mvy * 0.5
            mag = math.hypot(rvx, rvy)
            if mag > 0.3:
                scale = self.vmax / mag
                rvx *= scale
                rvy *= scale
        lx = mx + rvx * 0.45
        ly = my + rvy * 0.45
        sent = None
        tt = 999.0
        cap = self.cap
        rnd = 0
        while rnd < 2:
            gap = math.hypot(lx - bx, ly - by)
            if gap < 5.5:
                lx = _clamp(mx + rvx * 0.95, -46.0, 46.0)
                ly = _clamp(my + rvy * 0.95, -27.0, 27.0)
                gap = math.hypot(lx - bx, ly - by)
                if gap < 5.5:
                    return None
            ux, uy = _unit(lx - bx, ly - by)
            speed = gap / 1.35
            if speed < 13.0:
                speed = 13.0
            elif speed > 26.0:
                speed = 26.0
            sent = _send(bvx, bvy, ux, uy, speed, cap)
            if sent is None:
                return None
            ovx = bvx + sent[0] * sent[2] * cap
            ovy = bvy + sent[1] * sent[2] * cap
            if ovx < 0.8:
                return None
            aspd = math.hypot(ovx, ovy)
            tt = _flight(gap, aspd, self.dt, self.fr)
            if tt >= 140.0:
                return None
            sec = tt / self.hz
            if sec > 2.3:
                sec = 2.3
            elif sec < 0.35:
                sec = 0.35
            lx = _clamp(mx + rvx * sec, -46.0, 46.0)
            ly = _clamp(my + rvy * sec, -27.0, 27.0)
            rnd += 1
        if sent is None:
            return None
        gap = math.hypot(lx - bx, ly - by)
        if gap < 5.5 or gap > 58.0:
            return None
        return lx, ly, sent[2], tt, sent[0], sent[1]

    def _kickoff_kick(self, obs, kid, bx, by, bvx, bvy):
        shot = self._best_shot(bx, by, bvx, bvy, self.mx[kid])
        if shot is not None and shot[6] >= 2.5:
            return shot[1], shot[2], shot[3], shot[4], shot[5], -1, 0
        top = 0
        bot = 0
        j = 0
        while j < 5:
            if self.oy[j] >= 0.0:
                top += 1
            else:
                bot += 1
            j += 1
        want_pos = top <= bot
        mate = -1
        best = -1e9
        i = 1
        while i < 5:
            if i != kid:
                score = abs(self.my[i]) + self.mx[i] * 0.1
                if want_pos and self.my[i] > 0.0:
                    score += 8.0
                if not want_pos and self.my[i] < 0.0:
                    score += 8.0
                if score > best:
                    best = score
                    mate = i
            i += 1
        if mate < 0:
            return self._clear(bx, by, bvx, bvy, kid)
        side = 22.0 if self.my[mate] >= 0.0 else -22.0
        led = self._lead(bx, by, bvx, bvy, mate, 30.0, side)
        if led is None:
            lx = _clamp(self.mx[mate] + 16.0, 12.0, 28.0)
            ly = side
            ux, uy = _unit(lx - bx, ly - by)
            sent = _send(bvx, bvy, ux, uy, 20.0, self.cap)
            if sent is None:
                return ux, uy, 0.75, lx, ly, mate, 0
            pw = sent[2] if sent[2] >= 0.7 else 0.75
            return sent[0], sent[1], pw, lx, ly, mate, 0
        lx, ly, power, _tt, kdx, kdy = led
        pw = power if power >= 0.7 else 0.75
        return kdx, kdy, pw, lx, ly, mate, 0

    def _mouth_cross(self, bx, by, tx, ty):
        """True when the segment crosses our goal mouth."""
        if by * ty > 0.0 and abs(by) > 4.0:
            return False
        den = ty - by
        if den > -1e-6 and den < 1e-6:
            return abs(by) < self.mouth and bx < -30.0
        t = -by / den
        if t < 0.0 or t > 1.0:
            return False
        x = bx + (tx - bx) * t
        return x < -28.0

    def _gk_kick(self, obs, bx, by, bvx, bvy):
        best = None
        best_s = 6.5
        i = 1
        while i < 5:
            my = self.my[i]
            if abs(my) < 7.0:
                i += 1
                continue
            aimy = _clamp(my + (3.0 if my > 0.0 else -3.0), -24.0, 24.0)
            if abs(aimy) < 12.0:
                aimy = 16.0 if my > 0.0 else -16.0
            aimx = _clamp(self.mx[i] + 16.0, -6.0, 28.0)
            led = self._lead(bx, by, bvx, bvy, i, aimx, aimy)
            if led is None:
                i += 1
                continue
            lx, ly, power, tt, kdx, kdy = led
            if abs(ly) < 10.0 or self._mouth_cross(bx, by, lx, ly):
                i += 1
                continue
            ovx = bvx + kdx * power * self.cap
            if ovx < 1.0:
                i += 1
                continue
            dist = math.hypot(lx - bx, ly - by)
            room2 = self._lane(bx, by, lx, ly)
            need = 2.3 + 0.045 * dist
            if room2 < need * need:
                i += 1
                continue
            mine = _arrival(
                self.mx[i], self.my[i], self.mvx[i], self.mvy[i],
                lx, ly, self.reach2, self.reach, self.vmax, self.acc, self.hz,
            )
            theirs = self._point_eta(lx, ly, -1)
            if mine > theirs - 2.0 or mine > tt + 5.0:
                i += 1
                continue
            score = math.sqrt(room2) + abs(ly) * 0.12 + (theirs - mine) * 0.06
            if score > best_s:
                best_s = score
                pw = power if power >= 0.6 else 0.72
                best = (kdx, kdy, pw, lx, ly, i, 0)
            i += 1
        if best is not None:
            return self._guard(best, bx, by, bvx, bvy)
        return self._guard(self._touch_clear(bx, by, bvx, bvy), bx, by, bvx, bvy)

    def _net_bound(self, bx, by, bvx, bvy, kdx, kdy, power):
        rvx = bvx + kdx * power * self.cap
        rvy = bvy + kdy * power * self.cap
        if rvx >= -0.8:
            return False
        if bx <= self.gx + 0.4:
            return abs(by) < self.mouth + 1.0
        cy = by + rvy * ((self.gx - bx) / rvx)
        return abs(cy) < self.mouth + 0.8

    def _punch(self, bx, by, bvx, bvy):
        """Hard forward contact. Used when a softer kick would enter our net."""
        uy = -0.22 if by > 1.0 else 0.22 if by < -1.0 else 0.0
        ux, uy = _unit(1.0, uy)
        sent = _send(bvx, bvy, ux, uy, 30.0, self.cap)
        if sent is None or sent[0] < 0.5:
            return 1.0, 0.0, 1.0, bx + 22.0, 0.0, -1, 0
        return sent[0], sent[1], sent[2], bx + 24.0, uy * 16.0, -1, 0

    def _guard(self, kick, bx, by, bvx, bvy):
        if kick is None:
            return None
        if bx < -6.0 and self._net_bound(bx, by, bvx, bvy, kick[0], kick[1], kick[2]):
            return self._punch(bx, by, bvx, bvy)
        return kick

    def _touch_clear(self, bx, by, bvx, bvy):
        """Hard clearance down a touchline. Never across the goal mouth."""
        if by > 2.5:
            sides = (26.0,)
        elif by < -2.5:
            sides = (-26.0,)
        else:
            sides = (26.0, -26.0)
        best = None
        best_r = -1.0
        s = 0
        while s < len(sides):
            ty = sides[s]
            if abs(by) < 8.0:
                tx = _clamp(bx + 7.0, -42.0, -6.0)
            else:
                tx = _clamp(bx + 18.0, -36.0, 6.0)
            room = self._lane(bx, by, tx, ty)
            if by * ty < 0.0 and bx < -28.0:
                room = -1.0
            if room > best_r:
                best_r = room
                best = (tx, ty)
            s += 1
        if best is None:
            best = (_clamp(bx + 8.0, -40.0, 0.0), 26.0 if by >= 0.0 else -26.0)
        tx, ty = best
        ux, uy = _unit(tx - bx, ty - by)
        if ux < 0.05:
            ux, uy = _unit(0.45, 0.9 if ty > 0.0 else -0.9)
        sent = _send(bvx, bvy, ux, uy, 30.0, self.cap)
        if sent is None:
            return ux, uy, 1.0, tx, ty, -1, 0
        pw = sent[2] if sent[2] > 0.72 else 0.95
        return sent[0], sent[1], pw, tx, ty, -1, 0

    def _channel(self, bx, by, bvx, bvy, kid):
        """Drive at the open attacking corner instead of the near touchline."""
        best = None
        best_s = -1e18
        s = 0
        corners = (22.0, -22.0)
        while s < 2:
            ty = corners[s]
            tx = 46.0
            room2 = self._lane(bx, by, tx, ty)
            probe_x = bx * 0.35 + tx * 0.65
            probe_y = by * 0.35 + ty * 0.65
            theirs = self._point_eta(probe_x, probe_y, -1)
            mine = 1e9
            who = -1
            i = 1
            while i < 5:
                if i != kid:
                    eta = _arrival(
                        self.mx[i], self.my[i], self.mvx[i], self.mvy[i],
                        probe_x, probe_y, self.reach2, self.reach, self.vmax, self.acc, self.hz,
                    )
                    if eta < mine:
                        mine = eta
                        who = i
                i += 1
            my_eta = _arrival(
                self.mx[kid], self.my[kid], self.mvx[kid], self.mvy[kid],
                probe_x, probe_y, self.reach2, self.reach, self.vmax, self.acc, self.hz,
            )
            if my_eta < mine:
                mine = my_eta
                who = -1
            score = (theirs - mine) * 0.2 + room2 * 0.008
            if ty * by > 0.0:
                score += 1.5
            if score > best_s:
                best_s = score
                best = (tx, ty, who)
            s += 1
        tx, ty, who = best
        ux, uy = _unit(tx - bx, ty - by)
        if ux < 0.28:
            uy = 0.55 if ty >= 0.0 else -0.55
            ux, uy = _unit(0.85, uy)
        sent = _send(bvx, bvy, ux, uy, 28.0, self.cap)
        flag = 1 if who < 0 else 0
        if sent is None:
            return ux, uy, 0.9, tx, ty, who, flag
        pw = sent[2] if sent[2] > 0.62 else 0.85
        return sent[0], sent[1], pw, tx, ty, who, flag

    def _clear(self, bx, by, bvx, bvy, kid):
        if bvx < -7.0 and bx < -18.0:
            return self._punch(bx, by, bvx, bvy)
        if bx > -20.0:
            return self._channel(bx, by, bvx, bvy, kid)
        return self._touch_clear(bx, by, bvx, bvy)

    def _box_shot(self, bx, by, bvx, bvy):
        gk = 0
        j = 1
        while j < 5:
            if self.ox[j] > self.ox[gk]:
                gk = j
            j += 1
        post = self.mouth - 0.65
        ty = -post if self.oy[gk] >= by * 0.3 else post
        ux, uy = _unit(self.ogx - bx, ty - by)
        sent = _send(bvx, bvy, ux, uy, 30.0, self.cap)
        if sent is None or sent[0] <= 0.2:
            return None
        return sent[0], sent[1], sent[2], self.ogx, ty, -1, 0

    def _escape(self, kid, bx, by, bvx, bvy, px, py, opp):
        """Firm knock off a closing tackler. Power stays in 0.45..0.65."""
        ax = px - self.ox[opp]
        ay = py - self.oy[opp]
        away = _unit(ax + 1.6, ay)
        flank = 1.0 if self.oy[opp] <= py else -1.0
        cands = (away, (1.0, 0.0), _unit(0.85, flank * 0.7))
        best = None
        best_s = -1e18
        c = 0
        while c < 3:
            ux, uy = cands[c]
            if bx < -28.0 and ux < 0.45:
                c += 1
                continue
            if bx > -20.0 and ux < 0.2:
                c += 1
                continue
            tx = _clamp(bx + ux * 11.0, -44.0, 46.0)
            ty = _clamp(by + uy * 11.0, -25.0, 25.0)
            if ty > 24.0 or ty < -24.0:
                c += 1
                continue
            mine = _arrival(
                px, py, self.mvx[kid], self.mvy[kid],
                tx, ty, self.reach2, self.reach, self.vmax, self.acc, self.hz,
            )
            theirs = self._point_eta(tx, ty, -1)
            room2 = self._lane(bx, by, tx, ty)
            margin = theirs - mine
            score = margin * 2.0 + room2 * 0.04 + ux * 2.5
            if score > best_s:
                best_s = score
                best = (ux, uy, tx, ty, margin)
            c += 1
        if best is None or best[4] < 0.4:
            return None
        ux, uy, tx, ty, _margin = best
        sent = _send(bvx, bvy, ux, uy, 0.55 * self.cap, self.cap)
        if sent is None:
            return ux, uy, 0.55, tx, ty, -1, 1
        pw = sent[2]
        if pw < 0.45:
            pw = 0.45
        elif pw > 0.65:
            pw = 0.65
        return sent[0], sent[1], pw, tx, ty, -1, 1

    def _strike(self, obs, kid, bx, by, bvx, bvy, attacking):
        px = self.mx[kid]
        py = self.my[kid]
        near_i = 0
        near2 = 1e18
        j = 0
        while j < 5:
            dx = self.ox[j] - px
            dy = self.oy[j] - py
            d2 = dx * dx + dy * dy
            if d2 < near2:
                near2 = d2
                near_i = j
            j += 1
        pressed = near2 < 22.0
        rel_x = px - self.ox[near_i]
        rel_y = py - self.oy[near_i]
        closing = self.ovx[near_i] * rel_x + self.ovy[near_i] * rel_y
        hot = near2 < 12.96 and (closing > 0.35 or near2 < 6.76)
        if bx < -14.0 and bvx < -5.0:
            return self._guard(self._clear(bx, by, bvx, bvy, kid), bx, by, bvx, bvy)

        shot = self._best_shot(bx, by, bvx, bvy, px)
        pas = self._best_pass(obs, kid, bx, by, bvx, bvy, pressed or hot)

        def _take_shot():
            _m, dx, dy, pw, lx, ly, _margin = shot
            return self._guard((dx, dy, pw, lx, ly, -1, 0), bx, by, bvx, bvy)

        def _take_pass():
            _u, dx, dy, pw, lx, ly, mate = pas
            return self._guard((dx, dy, pw, lx, ly, mate, 0), bx, by, bvx, bvy)

        if hot:
            if shot is not None and (shot[6] >= 1.0 or bx > 12.0):
                return _take_shot()
            if pas is not None:
                return _take_pass()
            if shot is not None and bx > 8.0:
                return _take_shot()
            esc = self._escape(kid, bx, by, bvx, bvy, px, py, near_i)
            if esc is not None:
                return self._guard(esc, bx, by, bvx, bvy)
        else:
            if shot is not None and (pas is None or px > 4.0 or shot[6] >= 1.2):
                return _take_shot()
            if pas is not None:
                return _take_pass()
            if shot is not None:
                return _take_shot()
            carry = self._carry(kid, bx, by, bvx, bvy, px, py, pressed)
            if carry is not None:
                return self._guard(carry, bx, by, bvx, bvy)
        if bx > 25.0:
            boxed = self._box_shot(bx, by, bvx, bvy)
            if boxed is not None:
                return boxed
        return self._guard(self._clear(bx, by, bvx, bvy, kid), bx, by, bvx, bvy)

    def _best_shot(self, bx, by, bvx, bvy, px):
        ogx = self.ogx
        gk = 0
        gk_x = self.ox[0]
        j = 1
        while j < 5:
            if self.ox[j] > gk_x:
                gk_x = self.ox[j]
                gk = j
            j += 1
        if bx <= 5.0 and px <= 5.0 and gk_x > 34.0:
            return None
        if bx < -2.0:
            return None
        gkx = self.ox[gk]
        gky = self.oy[gk]
        gkvx = self.ovx[gk]
        gkvy = self.ovy[gk]
        best = None
        best_u = -1e9
        cap = self.cap
        k = 0
        ys = self.shot_y
        while k < 7:
            ty = ys[k]
            dx = ogx - bx
            dy = ty - by
            dist2 = dx * dx + dy * dy
            if dist2 < 1.0:
                k += 1
                continue
            dist = math.sqrt(dist2)
            ux = dx / dist
            uy = dy / dist
            if ux < 0.28:
                k += 1
                continue
            room2 = self._lane(bx, by, ogx, ty)
            need = 0.85 + 0.012 * dist
            if px > 14.0 and need > 1.45:
                need = 1.45
            if px > 30.0:
                need = 0.85
            open_lane = room2 >= need * need or room2 > 4.84 or (px > 24.0 and room2 > 0.90)
            sent = _send(bvx, bvy, ux, uy, 30.0, cap)
            if sent is None or sent[0] < 0.15:
                k += 1
                continue
            aspd = math.hypot(bvx + sent[0] * sent[2] * cap, bvy + sent[1] * sent[2] * cap)
            ball_eta = _flight(dist, aspd, self.dt, self.fr)
            if ball_eta > 80.0:
                k += 1
                continue
            k_eta = _arrival(
                gkx, gky, gkvx, gkvy, ogx, ty,
                self.reach2, self.reach, self.vmax, self.acc, self.hz,
            )
            margin = k_eta - ball_eta
            if not open_lane and margin < 1.2:
                k += 1
                continue
            if margin < -7.0 and bx < 18.0:
                k += 1
                continue
            score = margin * 1.2 + (0.5 if bx > 16.0 else 0.0)
            if room2 < 20.0:
                score += room2 * 0.03
            if score > best_u:
                best_u = score
                best = (score, sent[0], sent[1], sent[2], ogx, ty, margin)
            k += 1
        return best

    def _best_pass(self, obs, kid, bx, by, bvx, bvy, pressed):
        best = None
        best_u = 3.4
        i = 1
        while i < 5:
            if i != kid and (pressed or self.mx[i] > bx - 10.0):
                extra = 20.0 if self.mvx[i] > 0.55 else 12.0
                aimx = self.mx[i] + extra
                if aimx < bx + 7.0:
                    aimx = bx + 7.0
                aimx = _clamp(aimx, -16.0, 46.0)
                aimy = self.my[i]
                if self.mvx[i] > 0.4 and (aimy > 6.0 or aimy < -6.0):
                    aimy = _clamp(aimy + (2.0 if aimy > 0.0 else -2.0), -23.0, 23.0)
                else:
                    aimy = _clamp(aimy, -22.0, 22.0)
                led = self._lead(bx, by, bvx, bvy, i, aimx, aimy)
                if led is not None:
                    lx, ly, power, tt, kdx, kdy = led
                    gain = lx - bx
                    min_gain = 4.0 if pressed else 7.0
                    if gain >= min_gain:
                        dist = math.hypot(lx - bx, ly - by)
                        need = 2.05 + 0.055 * dist
                        room2 = self._lane(bx, by, lx, ly)
                        if room2 > need * need:
                            mine = _arrival(
                                self.mx[i], self.my[i], self.mvx[i], self.mvy[i],
                                lx, ly, self.reach2, self.reach, self.vmax, self.acc, self.hz,
                            )
                            theirs = self._point_eta(lx, ly, -1)
                            if (
                                mine <= tt + 4.0
                                and tt <= mine + 10.0
                                and mine + 3.0 <= theirs
                            ):
                                room = math.sqrt(room2)
                                utility = gain * 0.22 + (theirs - mine) * 0.1 + room * 0.4
                                ovx = bvx + kdx * power * self.cap
                                if utility > best_u and ovx > 0.8:
                                    pw = power if power >= 0.5 else 0.62
                                    best_u = utility
                                    best = (utility, kdx, kdy, pw, lx, ly, i)
            i += 1
        return best

    def _carry(self, kid, bx, by, bvx, bvy, px, py, pressed):
        if bx < -12.0 and bvx < -3.5:
            return None
        best = None
        best_s = -1e9
        step = 5.0 if pressed else 8.6
        offsets = (-9.0, -4.5, 0.0, 4.5, 9.0)
        k = 0
        while k < 5:
            tx = _clamp(px + step, -46.0, 46.0)
            ty = _clamp(py + offsets[k], -26.0, 26.0)
            # From our own box, never push back toward the net.
            if px < -30.0 and tx < px:
                tx = px + 4.0
            room2 = self._lane(bx, by, tx, ty)
            mine = _arrival(
                px, py, self.mvx[kid], self.mvy[kid], tx, ty,
                self.reach2, self.reach, self.vmax, self.acc, self.hz,
            )
            theirs = self._point_eta(tx, ty, -1)
            if theirs + 0.5 < mine:
                k += 1
                continue
            forward = tx - px
            score = room2 + forward * 8.0 + (theirs - mine) * 4.0
            if abs(ty) > 24.0:
                score -= 8.0
            if score > best_s:
                best_s = score
                best = (tx, ty, room2)
            k += 1
        if best is None:
            return None
        tx, ty, room2 = best
        if room2 < 2.4 and (pressed or px < -18.0):
            return None
        ux, uy = _unit(tx - bx, ty - by)
        if px < -28.0 and ux < 0.2:
            return None
        gap = math.hypot(tx - bx, ty - by)
        speed = _clamp(gap * 1.15, 5.4, 8.4)
        sent = _send(bvx, bvy, ux, uy, speed, self.cap)
        if sent is None:
            return ux, uy, 0.3, tx, ty, -1, 1
        power = sent[2]
        cap_pw = 0.46 if pressed else 0.36
        if power > cap_pw:
            power = cap_pw
        if power < 0.22:
            power = 0.22
        return sent[0], sent[1], power, tx, ty, -1, 1
