"""Meridian.

One outfield player meets the ball on its future path. A keeper lives on the
shot line. Everyone else is either the last defender or a runner. Kicks are
aimed as a change in ball velocity, not as a direction from the boot.
"""

from __future__ import annotations

import math

from soccer import TeamAction, TeamController

HORIZON = 45
STRIDE = 2
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
    if speed < 0.8:
        return 999.0
    reach = speed * dt / (1.0 - fr)
    if dist >= reach * 0.97:
        return 999.0
    ratio = 1.0 - dist / reach
    if ratio <= 0.002:
        return 999.0
    return math.log(ratio) / math.log(fr)


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
    mag = math.hypot(mx, my)
    if mag > 1.0:
        inv = 1.0 / mag
        return mx * inv, my * inv
    if mag != mag:
        return 0.0, 0.0
    return mx, my


class MyTeam(TeamController):
    name = "meridian"
    version = "2"

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
        self.oeta = [0.0] * 5
        self.shot_y = (0.0,) * 7

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
        self.xlim = field.width * 0.5 - field.ball_radius
        self.ylim = field.height * 0.5 - field.ball_radius
        self.mouth = field.goal_width * 0.5
        self.post = self.mouth - 0.8
        self.ccr = field.centre_circle_radius
        self.pr = field.player_radius
        self.safe = self.ccr + self.pr + 1.5
        self.gx = field.my_goal[0]
        self.ogx = field.opponent_goal[0]
        half = self.post
        self.shot_y = tuple(half * i / 3.0 for i in range(-3, 4))
        self.ready = True

    def _load(self, players, xs, ys, vxs, vys):
        n = len(players)
        if n > 5:
            n = 5
        i = 0
        while i < n:
            p = players[i]
            pos = p.position
            vel = p.velocity
            xs[i] = pos[0]
            ys[i] = pos[1]
            vxs[i] = vel[0]
            vys[i] = vel[1]
            i += 1
        return n

    def _roll(self, bx, by, bvx, bvy):
        dt = self.dt
        fr = self.fr
        xlim = self.xlim
        ylim = self.ylim
        mouth = self.mouth
        hx = self.hx
        hy = self.hy
        i = 0
        while i < HORIZON:
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
            i += 1

    def _meet(self, px, py, vx, vy):
        """Earliest path index the player can reach, and the arrival tick."""
        hx = self.hx
        hy = self.hy
        reach = self.reach
        reach2 = self.reach2
        vmax = self.vmax
        acc = self.acc
        hz = self.hz
        dt = self.dt
        t = 0
        found = -1
        while t < HORIZON:
            dx = hx[t] - px
            dy = hy[t] - py
            lim = reach + vmax * (t * dt)
            if dx * dx + dy * dy <= lim * lim:
                eta = _arrival(px, py, vx, vy, hx[t], hy[t], reach2, reach, vmax, acc, hz)
                if eta <= t + 0.45:
                    found = t
                    break
            t += STRIDE
        if found >= 0:
            best = found
            lo = found - STRIDE + 1
            if lo < 0:
                lo = 0
            t = found - 1
            while t >= lo:
                eta = _arrival(px, py, vx, vy, hx[t], hy[t], reach2, reach, vmax, acc, hz)
                if eta <= t + 0.45:
                    best = t
                else:
                    break
                t -= 1
            return best, float(best)
        best_i = 0
        best_d = 1e18
        t = 0
        while t < HORIZON:
            dx = hx[t] - px
            dy = hy[t] - py
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best_i = t
            t += STRIDE
        eta = _arrival(px, py, vx, vy, hx[best_i], hy[best_i], reach2, reach, vmax, acc, hz)
        if eta < float(best_i):
            eta = float(best_i)
        return best_i, eta

    def _shot_cross(self, bx, by, bvx, bvy):
        """Y where a goal-bound ball crosses the keeper's line. None if it misses."""
        line = self.gx + 1.5
        mouth = self.mouth
        if bvx < -1.0 and bx > line:
            cy = by + bvy * ((line - bx) / bvx)
            if cy <= mouth + 2.0 and cy >= -(mouth + 2.0) and cy < self.ylim and cy > -self.ylim:
                return cy
        if bvx >= -0.4 or bx <= line:
            return None
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
                if cy <= mouth + 2.0 and cy >= -(mouth + 2.0):
                    return cy
                return None
            prev_x = x
            prev_y = y
            i += 1
        return None

    def _gk_spot(self, bx, by, bvx, bvy, cross):
        gx = self.gx
        gkx = self.mx[0]
        post = self.mouth - 0.45
        # A ball already behind the keeper stays there. Chasing it through
        # the six-yard box is how own goals happen.
        past = bx < gkx - 0.15 and bx < -46.2 and abs(by) < self.mouth + 1.2
        if cross is not None:
            y = _clamp(cross, -post, post)
            x = gkx if past else gx + 1.35
            return x, y
        if bx < -30.0:
            y = _clamp(by + bvy * 0.2, -post, post)
            if bx < -38.0:
                x = gx + 1.6
            else:
                x = -47.3
            if past:
                x = gkx
            return x, y
        shade = by * (3.4 / max(bx - gx, 14.0))
        return -47.6, _clamp(shade, -3.6, 3.6)

    def act(self, obs):
        if not self.ready:
            self._boot(obs.field)
        me = obs.my_players
        them = obs.opponents
        n = self._load(me, self.mx, self.my, self.mvx, self.mvy)
        self._load(them, self.ox, self.oy, self.ovx, self.ovy)
        ball = obs.ball.position
        vel = obs.ball.velocity
        bx = ball[0]
        by = ball[1]
        bvx = vel[0]
        bvy = vel[1]
        self._roll(bx, by, bvx, bvy)

        spd2 = bvx * bvx + bvy * bvy
        centre2 = bx * bx + by * by
        ctrl = obs.ball.controlling_team
        their_ko = ctrl == 1 and centre2 < 4.0 and spd2 < 0.25
        our_ko = ctrl == 0 and centre2 < 4.0 and spd2 < 0.25
        if their_ko:
            self.lock_id = -1
            return self._ring(obs)

        mx = self.mx
        my = self.my
        mvx = self.mvx
        mvy = self.mvy
        meta = self.meta
        midx = self.midx
        oeta = self.oeta

        i = 1
        while i < n:
            midx[i], meta[i] = self._meet(mx[i], my[i], mvx[i], mvy[i])
            i += 1
        j = 0
        opp_best = 1e9
        on = len(them)
        if on > 5:
            on = 5
        while j < on:
            _, eta = self._meet(self.ox[j], self.oy[j], self.ovx[j], self.ovy[j])
            oeta[j] = eta
            if eta < opp_best:
                opp_best = eta
            j += 1

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
            gk_idx, gk_eta = self._meet(mx[0], my[0], mvx[0], mvy[0])
        cross = self._shot_cross(bx, by, bvx, bvy)
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

        spots_x = [0.0] * 5
        spots_y = [0.0] * 5
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
        if locked > 0 and locked != kicker and locked != hunter:
            spots_x[locked] = self.lock_x
            spots_y[locked] = self.lock_y

        actions = TeamAction()
        i = 0
        while i < n:
            if i == 0:
                if kick is not None and kicker == 0:
                    actions.kick(0, (kick[0], kick[1]), kick[2], movement=_toward(mx[0], my[0], bx, by, False))
                else:
                    ease = cross is None and bx > -32.0
                    actions.move(0, _sanitize(_toward(mx[0], my[0], spots_x[0], spots_y[0], ease)))
                i += 1
                continue
            if kick is not None and i == kicker:
                if kick[6] == 1:
                    mv = self._dribble_move(i, kick[3], kick[4])
                else:
                    mv = _toward(mx[i], my[i], bx, by, False)
                actions.kick(i, (kick[0], kick[1]), kick[2], movement=_sanitize(mv))
            else:
                chase = i == hunter and i != kicker
                actions.move(i, _sanitize(_toward(mx[i], my[i], spots_x[i], spots_y[i], not chase)))
            i += 1
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
            ya = 21.0 if self.my[a] >= 0.0 else -21.0
            sx[a] = 28.0
            sy[a] = ya
            if b >= 0:
                yb = -21.0 if ya > 0.0 else 21.0
                sx[b] = 24.0
                sy[b] = yb
            return
        if bx > 14.0:
            rx = 42.0
            ry = -5.6 if by >= 0.0 else 5.6
        elif bx > -8.0:
            rx = _clamp(bx + 16.0, 8.0, 40.0)
            ry = -17.0 if by >= 0.0 else 17.0
        else:
            rx = _clamp(bx + 22.0, 2.0, 20.0)
            ry = -18.0 if by >= 0.0 else 18.0
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
        if a >= 0 and b >= 0 and t0 >= 0 and t1 >= 0:
            s0x, s0y = self._mark(t0, bx, by)
            s1x, s1y = self._mark(t1, bx, by)
            keep = (self.mx[a] - s0x) ** 2 + (self.my[a] - s0y) ** 2
            keep += (self.mx[b] - s1x) ** 2 + (self.my[b] - s1y) ** 2
            swap = (self.mx[a] - s1x) ** 2 + (self.my[a] - s1y) ** 2
            swap += (self.mx[b] - s0x) ** 2 + (self.my[b] - s0y) ** 2
            if swap < keep:
                sx[a], sy[a] = s1x, s1y
                sx[b], sy[b] = s0x, s0y
            else:
                sx[a], sy[a] = s0x, s0y
                sx[b], sy[b] = s1x, s1y

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
                sx = _clamp(sx, -44.0, -12.0)
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
        best = 1e9
        j = 0
        while j < 5:
            if j != skip:
                eta = _arrival(
                    self.ox[j], self.oy[j], self.ovx[j], self.ovy[j],
                    tx, ty, reach2, reach, vmax, acc, hz,
                )
                if eta < best:
                    best = eta
            j += 1
        return best

    def _lead(self, bx, by, bvx, bvy, mate, aimx, aimy):
        """Two-step lead. Timing uses the speed the kick will actually produce."""
        mx = self.mx[mate]
        my = self.my[mate]
        dx = aimx - mx
        dy = aimy - my
        dist = math.hypot(dx, dy)
        if dist < 0.8:
            rvx, rvy = 1.0, 0.0
        else:
            rvx = dx / dist
            rvy = dy / dist
        rvx *= self.vmax
        rvy *= self.vmax
        lx = mx + rvx * 0.4
        ly = my + rvy * 0.4
        sent = None
        tt = 999.0
        rnd = 0
        while rnd < 2:
            gap = math.hypot(lx - bx, ly - by)
            if gap < 6.0:
                return None
            ux, uy = _unit(lx - bx, ly - by)
            speed = _clamp(gap / 1.5, 12.0, 24.0)
            sent = _send(bvx, bvy, ux, uy, speed, self.cap)
            if sent is None or sent[0] < -0.05:
                return None
            aspd = math.hypot(
                bvx + sent[0] * sent[2] * self.cap,
                bvy + sent[1] * sent[2] * self.cap,
            )
            tt = _flight(gap, aspd, self.dt, self.fr)
            if tt >= 150.0:
                return None
            sec = tt / self.hz
            if sec > 2.6:
                sec = 2.6
            lx = _clamp(mx + rvx * sec, -46.0, 46.0)
            ly = _clamp(my + rvy * sec, -27.0, 27.0)
            rnd += 1
        if sent is None:
            return None
        gap = math.hypot(lx - bx, ly - by)
        if gap < 6.0 or gap > 58.0:
            return None
        return lx, ly, sent[2], tt, sent[0], sent[1]

    def _kickoff_kick(self, obs, kid, bx, by, bvx, bvy):
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
        side = 20.0 if self.my[mate] >= 0.0 else -20.0
        led = self._lead(bx, by, bvx, bvy, mate, 32.0, side)
        if led is None:
            lx = _clamp(self.mx[mate] + 14.0, 8.0, 36.0)
            ly = side
            ux, uy = _unit(lx - bx, ly - by)
            sent = _send(bvx, bvy, ux, uy, 16.0, self.cap)
            if sent is None:
                return ux, uy, 0.7, lx, ly, mate, 0
            pw = sent[2] if sent[2] >= 0.5 else 0.62
            return sent[0], sent[1], pw, lx, ly, mate, 0
        lx, ly, power, _tt, kdx, kdy = led
        pw = power if power >= 0.5 else 0.62
        return kdx, kdy, pw, lx, ly, mate, 0

    def _gk_kick(self, obs, bx, by, bvx, bvy):
        best = -1
        best_s = -1e18
        i = 1
        while i < 5:
            space = 1e18
            j = 0
            while j < 5:
                dx = self.mx[i] - self.ox[j]
                dy = self.my[i] - self.oy[j]
                d2 = dx * dx + dy * dy
                if d2 < space:
                    space = d2
                j += 1
            score = space + abs(self.my[i]) * 18.0 + self.mx[i] * 3.0
            if abs(self.my[i]) < 5.0:
                score -= 80.0
            if score > best_s:
                best_s = score
                best = i
            i += 1
        if best >= 0 and best_s > 70.0:
            side = 16.0 if self.my[best] >= 0.0 else -16.0
            if abs(self.my[best]) > 8.0:
                side = _clamp(self.my[best], -22.0, 22.0)
            led = self._lead(bx, by, bvx, bvy, best, _clamp(self.mx[best] + 18.0, -8.0, 34.0), side)
            if led is not None:
                lx, ly, power, _tt, kdx, kdy = led
                room2 = self._lane(bx, by, lx, ly)
                need = 2.0 + 0.06 * math.hypot(lx - bx, ly - by)
                if room2 > need * need and abs(ly) > 6.0 and kdx > 0.05:
                    pw = power if power >= 0.5 else 0.62
                    return self._guard((kdx, kdy, pw, lx, ly, best, 0), bx, by, bvx, bvy)
        return self._guard(self._clear(bx, by, bvx, bvy, 0), bx, by, bvx, bvy)

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

    def _clear(self, bx, by, bvx, bvy, kid):
        if bvx < -7.0 and bx < -18.0:
            return self._punch(bx, by, bvx, bvy)
        left = 0
        right = 0
        j = 0
        while j < 5:
            if self.ox[j] < bx + 18.0:
                if self.oy[j] >= 0.0:
                    left += 1
                else:
                    right += 1
            j += 1
        side = -25.5 if left > right else 25.5
        tx = _clamp(bx + 30.0, -12.0, 28.0)
        ty = side
        # Prefer the flank whose lane is actually emptier.
        room_a = self._lane(bx, by, tx, ty)
        other = -side
        room_b = self._lane(bx, by, tx, other)
        if room_b > room_a:
            ty = other
        ux, uy = _unit(tx - bx, ty - by)
        if ux < 0.05:
            ux, uy = _unit(1.0, ty)
        sent = _send(bvx, bvy, ux, uy, 26.0, self.cap)
        if sent is None:
            return ux, uy, 0.95, tx, ty, -1, 0
        return sent[0], sent[1], sent[2] if sent[2] > 0.55 else 0.9, tx, ty, -1, 0

    def _strike(self, obs, kid, bx, by, bvx, bvy, attacking):
        px = self.mx[kid]
        py = self.my[kid]
        near2 = 1e18
        j = 0
        while j < 5:
            dx = self.ox[j] - px
            dy = self.oy[j] - py
            d2 = dx * dx + dy * dy
            if d2 < near2:
                near2 = d2
            j += 1
        pressed = near2 < 22.0
        if bx < -14.0 and bvx < -5.0:
            return self._guard(self._clear(bx, by, bvx, bvy, kid), bx, by, bvx, bvy)

        shot = self._best_shot(bx, by, bvx, bvy, px)
        pas = self._best_pass(obs, kid, bx, by, bvx, bvy, pressed)
        # In the attacking half a shot beats a speculative pass. Bad passes
        # are how the other team gets its chances.
        if shot is not None and (pas is None or px > 4.0 or shot[0] + 1.2 >= pas[0]):
            _u, dx, dy, pw, lx, ly = shot
            return self._guard((dx, dy, pw, lx, ly, -1, 0), bx, by, bvx, bvy)
        if pas is not None:
            _u, dx, dy, pw, lx, ly, mate = pas
            return self._guard((dx, dy, pw, lx, ly, mate, 0), bx, by, bvx, bvy)
        if shot is not None:
            _u, dx, dy, pw, lx, ly = shot
            return self._guard((dx, dy, pw, lx, ly, -1, 0), bx, by, bvx, bvy)

        carry = self._carry(kid, bx, by, bvx, bvy, px, py, pressed)
        if carry is not None:
            return self._guard(carry, bx, by, bvx, bvy)
        return self._guard(self._clear(bx, by, bvx, bvy, kid), bx, by, bvx, bvy)

    def _best_shot(self, bx, by, bvx, bvy, px):
        if px <= 5.0 and bx <= 5.0:
            return None
        ogx = self.ogx
        gk = 0
        gk_x = self.ox[0]
        j = 1
        while j < 5:
            if self.ox[j] > gk_x:
                gk_x = self.ox[j]
                gk = j
            j += 1
        gk_y = self.oy[gk]
        gk_deep = gk_x > 32.0
        best = None
        best_u = -1e9
        k = 0
        ys = self.shot_y
        while k < 7:
            ty = ys[k]
            dx = ogx - bx
            dy = ty - by
            dist = math.hypot(dx, dy)
            if dist < 1.0:
                k += 1
                continue
            ux = dx / dist
            uy = dy / dist
            room2 = self._lane(bx, by, ogx, ty)
            room = math.sqrt(room2)
            need = 0.85 + 0.012 * dist
            if px > 14.0:
                need = min(need, 1.45)
            if px > 30.0:
                need = 0.85
            penalty = 0.0
            if gk_deep:
                gap = abs(ty - gk_y)
                if gap < 4.2:
                    penalty = (4.2 - gap) * 0.9
            score = room - penalty + 0.12 * abs(ty)
            open_shot = room >= need or room > 2.2 or (px > 24.0 and room > 0.95)
            if open_shot and ux > 0.25 and score > best_u:
                sent = _send(bvx, bvy, ux, uy, 30.0, self.cap)
                if sent is not None and sent[0] > 0.2:
                    best_u = score + px * 0.03
                    best = (best_u, sent[0], sent[1], sent[2], ogx, ty)
            k += 1
        return best

    def _best_pass(self, obs, kid, bx, by, bvx, bvy, pressed):
        best = None
        best_u = 5.2
        i = 1
        while i < 5:
            if i != kid:
                aimx = self.mx[i] + 13.0
                if aimx < bx + 8.0:
                    aimx = bx + 8.0
                aimx = _clamp(aimx, -20.0, 45.0)
                aimy = _clamp(self.my[i], -22.0, 22.0)
                led = self._lead(bx, by, bvx, bvy, i, aimx, aimy)
                if led is not None:
                    lx, ly, power, tt, kdx, kdy = led
                    gain = lx - bx
                    min_gain = 4.0 if pressed else 8.0
                    if gain >= min_gain:
                        dist = math.hypot(lx - bx, ly - by)
                        room2 = self._lane(bx, by, lx, ly)
                        need = 2.2 + 0.07 * dist
                        if room2 > need * need:
                            mine = _arrival(
                                self.mx[i], self.my[i], self.mvx[i], self.mvy[i],
                                lx, ly, self.reach2, self.reach, self.vmax, self.acc, self.hz,
                            )
                            theirs = self._point_eta(lx, ly, -1)
                            if mine <= tt + 4.0 and mine + 2.0 >= tt * 0.45 and mine < theirs - 6.0:
                                room = math.sqrt(room2)
                                utility = gain * 0.22 + room * 0.45 + (theirs - mine) * 0.05
                                if utility > best_u and kdx > -0.2:
                                    pw = power if power >= 0.5 else 0.58
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
        if power > 0.38:
            power = 0.38
        if power < 0.22:
            power = 0.22
        return sent[0], sent[1], power, tx, ty, -1, 1
