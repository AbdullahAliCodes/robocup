"""Interception-driven team.

Every decision is built on one primitive: given a ball trajectory, when can
each player first reach it? From that we pick the chaser, judge whose ball it
is, and score every candidate kick (shots, passes, dribbles) by who wins the
race to the ball afterwards and where.
"""

import math

from soccer import PlayerAction, TeamAction, TeamController

DT = 0.05
FRIC = 0.985
HALF_W = 50.0
HALF_H = 30.0
GOAL_HALF = 7.0
KICK_IMPULSE = 22.0
BALL_MAX = 30.0
REACH = 2.0           # how close counts as "got there" (kick range is 2.2)
RUN = 0.4             # field units per tick at top speed
CIRCLE = 9.0
RESTART_TICKS = 61

# Tick offsets at which a candidate kick's path is sampled.
SAMPLES = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 26, 31, 37, 44, 52)
# Pessimism: assume opponents reach a kicked ball 9 ticks sooner than the
# model says. Tuned in self-play; it favours safe, territory-gaining kicks.
OPP_DELAY = -9.0


def _sig(x):
    if x > 30:
        return 1.0
    if x < -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def reach_ticks(px, py, vx, vy, tx, ty):
    """Ticks for a player at (px,py) moving (vx,vy) to get within REACH of (tx,ty)."""
    dx = tx - px
    dy = ty - py
    d = math.sqrt(dx * dx + dy * dy)
    de = d - REACH
    if de <= 0.0:
        return 0.0
    s0 = (vx * dx + vy * dy) / d
    lag = 6.0
    if de < 3.0:
        lag *= math.sqrt(de / 3.0)
    if s0 < 0.0:
        lag -= s0
    else:
        lag -= 0.72 * s0
        if lag < 0.0:
            lag = 0.0
    w = vx * dy - vy * dx
    lag += 0.1 * abs(w) / d
    return de / RUN + lag


def ball_path(x, y, vx, vy, n):
    """Ball positions for ticks 0..n, with wall rebounds (goal mouth is open)."""
    out = [(x, y)]
    lim_y = HALF_H - 0.45
    lim_x = HALF_W - 0.45
    for _ in range(n):
        x += vx * DT
        y += vy * DT
        vx *= FRIC
        vy *= FRIC
        if y > lim_y:
            y = 2 * lim_y - y
            vy = -vy * 0.75
        elif y < -lim_y:
            y = -2 * lim_y - y
            vy = -vy * 0.75
        if (x > lim_x or x < -lim_x) and abs(y) > GOAL_HALF:
            if x > lim_x:
                x = 2 * lim_x - x
            else:
                x = -2 * lim_x - x
            vx = -vx * 0.75
        out.append((x, y))
    return out


def norm(x, y):
    d = math.sqrt(x * x + y * y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


def kick_for(bvx, bvy, ux, uy, speed):
    """Kick (direction, power) that leaves the ball moving at speed along u.

    The impulse is added to the ball's velocity, so aim at the difference.
    Returns (dir, power, achieved_speed) or None if impossible.
    """
    speed = min(speed, BALL_MAX)
    kx = ux * speed - bvx
    ky = uy * speed - bvy
    k = math.sqrt(kx * kx + ky * ky)
    if k > KICK_IMPULSE:
        # Fastest achievable speed in that direction.
        b = ux * bvx + uy * bvy
        disc = b * b - (bvx * bvx + bvy * bvy) + KICK_IMPULSE ** 2
        if disc < 0:
            return None
        speed = min(BALL_MAX, b + math.sqrt(disc)) * 0.999
        if speed <= 1.0:
            return None
        kx = ux * speed - bvx
        ky = uy * speed - bvy
        k = math.sqrt(kx * kx + ky * ky)
    if k < 1e-6:
        return (ux, uy), 0.0, speed
    return (kx / k, ky / k), min(1.0, k / KICK_IMPULSE), speed


def value_ours(x, y):
    """How good it is for us to have the ball at (x, y)."""
    prog = (x + HALF_W) / (2 * HALF_W)
    dg = math.hypot(HALF_W - x, y)
    v = 0.15 + 0.55 * prog * prog
    if dg < 30.0:
        v += 0.35 * (1.0 - dg / 30.0)
    v -= 0.1 * abs(y) / HALF_H
    return v


def value_theirs(x, y):
    """How bad it is for them to have the ball at (x, y) (negative)."""
    back = (HALF_W - x) / (2 * HALF_W)
    dg = math.hypot(-HALF_W - x, y)
    v = -0.15 - 0.55 * back * back
    if dg < 30.0:
        v -= 0.35 * (1.0 - dg / 30.0)
    return v


class MyTeam(TeamController):
    name = "intercept_fc"
    version = "8"

    def initial_formation(self, field):
        return [
            (-47.0, 0.0),
            (-26.0, -9.0),
            (-26.0, 9.0),
            (-1.0, -12.0),
            (-9.6, 1.0),
        ]

    def __init__(self):
        self.chaser = None
        self.restart_tick = 0

    def reset(self, seed):
        self.chaser = None
        self.restart_tick = 0

    # ------------------------------------------------------------------

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception:
            # Never forfeit over a bug: fall back to a plain, legal team.
            return self._fallback(obs)

    def _fallback(self, obs):
        actions = TeamAction()
        bx, by = obs.ball.position
        chaser = obs.closest_my_player_to((bx, by))
        for p in obs.my_players:
            px, py = p.position
            if chaser is not None and p.id == chaser.id:
                if obs.can_kick(p.id):
                    actions.kick(p.id, norm(HALF_W - bx, -by), 1.0)
                else:
                    actions.move(p.id, norm(bx - px, by - py))
            else:
                actions.set(p.id, PlayerAction())
        return actions

    def _act(self, obs):
        me = obs.my_players
        them = obs.opponents
        n = len(me)
        bx, by = obs.ball.position
        bvx, bvy = obs.ball.velocity
        path = ball_path(bx, by, bvx, bvy, 80)

        # Earliest interception tick for every player on the free ball path.
        def intercept(p, delay=0.0):
            px, py = p.position
            vx, vy = p.velocity
            for k in range(0, 81, 2):
                tx, ty = path[k]
                if reach_ticks(px, py, vx, vy, tx, ty) <= k + delay:
                    return k
            return 81

        my_t = [intercept(p) for p in me]
        their_t = [intercept(p) for p in them]
        best_them = min(their_t) if their_t else 99
        # Chaser: earliest field player, with hysteresis. Keeper only near goal.
        cands = []
        for i, p in enumerate(me):
            t = my_t[i]
            if i == 0:
                gx, gy = path[min(t, 80)]
                if gx > -32 or abs(gy) > 20:
                    t += 100
            cands.append((t, i))
        cands.sort()
        chaser = cands[0][1]
        if self.chaser is not None and self.chaser < n and self.chaser != chaser:
            if cands_t(cands, self.chaser) <= cands[0][0] + 2:
                chaser = self.chaser
        self.chaser = chaser
        t_us = my_t[chaser]
        ours = t_us + 1 < best_them

        # Restart awareness: their kickoff, ball dead on the spot.
        for e in obs.events:
            if e.get("kind") == "goal":
                self.restart_tick = obs.tick
        dead_centre = (abs(bx) < 0.2 and abs(by) < 0.2 and abs(bvx) + abs(bvy) < 0.05)
        their_restart = (dead_centre and obs.ball.controlling_team != 0
                         and obs.tick - self.restart_tick < RESTART_TICKS)

        actions = TeamAction()
        targets = {}

        # --- the chaser --------------------------------------------------
        if not their_restart:
            cp = me[chaser]
            if obs.can_kick(chaser):
                kick = self.choose_kick(obs, chaser)
                if kick is not None:
                    d, pw, mv = kick
                    actions.kick(chaser, d, pw, movement=mv)
                else:
                    actions.move(chaser, norm(bx - cp.position[0], by - cp.position[1]))
            else:
                tx, ty = path[min(t_us, 80)]
                targets[chaser] = (tx, ty)
        # --- keeper ------------------------------------------------------
        if chaser != 0 or their_restart:
            targets[0] = self.keeper_spot(obs, path)

        # --- everyone else ----------------------------------------------
        others = [p for p in me if p.id != 0 and (p.id != chaser or their_restart)]
        if ours:
            spots = self.attack_spots(obs, len(others))
        else:
            spots = self.defend_spots(obs, len(others), chaser if not their_restart else None)
        for pid, spot in assign(others, spots).items():
            targets[pid] = spot

        for pid, (tx, ty) in targets.items():
            if their_restart or dead_centre:
                tx, ty = outside_circle(tx, ty, CIRCLE + 1.2 if their_restart else 0.0)
            p = me[pid]
            mv = arrive(p, tx, ty)
            if their_restart:
                mv = steer_clear(p, mv, CIRCLE + 1.0)
            actions.move(pid, mv)
        for p in me:
            if p.id not in actions.players:
                actions.set(p.id, PlayerAction())
        return actions

    # ------------------------------------------------------------------

    def keeper_spot(self, obs, path):
        gx = -HALF_W
        # Incoming shot: meet it on the line.
        kp = obs.my_players[0]
        px, py = kp.position
        pvx, pvy = kp.velocity
        for k in range(1, 60):
            x, y = path[k]
            if x < -HALF_W + 1.0:
                if abs(y) < GOAL_HALF + 1.5:
                    # Shot on target: the earliest point on its path we can reach.
                    for j in range(1, k + 1):
                        qx, qy = path[j]
                        if qx < -HALF_W + 12 and reach_ticks(px, py, pvx, pvy, qx, qy) <= j:
                            return (qx, qy)
                    return (-HALF_W + 1.0, max(-6.5, min(6.5, y)))
                break
        bx, by = obs.ball.position
        dx, dy = bx - gx, by
        d = math.hypot(dx, dy)
        r = 2.5 if d > 30 else 2.0 + 0.1 * max(0.0, 30 - d) * 0.5
        ux, uy = norm(dx, dy)
        x = gx + ux * r
        y = uy * r * 1.6
        return (max(-HALF_W + 1.2, x), max(-6.0, min(6.0, y)))

    def attack_spots(self, obs, count):
        bx, by = obs.ball.position
        fx = min(42.0, bx + 14.0)
        spots = [
            (fx, max(-22.0, min(22.0, -14.0 if by > 0 else 14.0) + by * 0.3)),
            (min(40.0, bx + 8.0), by * 0.4 + (6.0 if by <= 0 else -6.0)),
            (min(30.0, max(-36.0, bx - 14.0)), by * 0.3),
            (max(-25.0, bx - 8.0), -by * 0.3),
        ]
        out = []
        for (x, y) in spots[:count]:
            x, y = self.find_space(obs, x, y)
            out.append((x, y))
        return out

    def find_space(self, obs, x, y):
        for o in obs.opponents:
            ox, oy = o.position
            dx, dy = x - ox, y - oy
            d = math.hypot(dx, dy)
            if d < 6.0:
                ux, uy = norm(dx, dy) if d > 1e-6 else (0.0, 1.0)
                x += ux * (6.0 - d) * 0.8
                y += uy * (6.0 - d) * 0.8
        return (max(-47.0, min(47.0, x)), max(-27.0, min(27.0, y)))

    def defend_spots(self, obs, count, chaser):
        bx, by = obs.ball.position
        gx = -HALF_W
        spots = []
        # Cover: goalside of the ball.
        ux, uy = norm(gx - bx, -by)
        cover = (bx + ux * 9.0, by + uy * 9.0)
        spots.append(cover)
        # Markers on the most dangerous opponents not on the ball.
        carrier = obs.closest_opponent_to((bx, by))
        threats = []
        for o in obs.opponents:
            if carrier is not None and o.id == carrier.id:
                continue
            ox, oy = o.position
            danger = -math.hypot(gx - ox, oy)
            threats.append((danger, ox, oy))
        threats.sort(reverse=True)
        for _, ox, oy in threats:
            if len(spots) >= count:
                break
            ux, uy = norm(gx - ox, -oy)
            # Goalside and a little towards the ball.
            bxu, byu = norm(bx - ox, by - oy)
            mx = ox + ux * 2.5 + bxu * 1.0
            my = oy + uy * 2.5 + byu * 1.0
            mx = max(-46.0, min(mx, bx - 2.0))
            spots.append((mx, my))
        while len(spots) < count:
            spots.append((-35.0, 0.0))
        return spots[:count]

    # ------------------------------------------------------------------

    def choose_kick(self, obs, kid):
        me = obs.my_players
        them = obs.opponents
        k_p = me[kid]
        bx, by = obs.ball.position
        bvx, bvy = obs.ball.velocity
        mates = [(p.position[0], p.position[1], p.velocity[0], p.velocity[1], p.id) for p in me]
        opps = [(p.position[0], p.position[1], p.velocity[0], p.velocity[1]) for p in them]

        cands = []
        # Shots across the mouth.
        for gy in (-5.5, -3.5, -1.5, 1.5, 3.5, 5.5):
            ux, uy = norm(HALF_W + 1.0 - bx, gy - by)
            cands.append((ux, uy, 30.0, "shot"))
        # Passes to team-mates, led.
        for p in me:
            if p.id == kid:
                continue
            for spd in (12.0, 18.0, 25.0):
                tx, ty = p.position[0], p.position[1]
                for _ in range(2):
                    dist = math.hypot(tx - bx, ty - by)
                    tt = travel_ticks(dist, spd)
                    tx = p.position[0] + p.velocity[0] * DT * tt * 0.7
                    ty = p.position[1] + p.velocity[1] * DT * tt * 0.7
                ux, uy = norm(tx - bx, ty - by)
                cands.append((ux, uy, spd, "pass"))
        # Dribbles / pushes in a fan, biased forward.
        for ang in (-90, -60, -35, -15, 0, 15, 35, 60, 90, 135, -135, 180):
            a = math.radians(ang)
            ux, uy = math.cos(a), math.sin(a)
            for spd in (9.5, 16.0):
                cands.append((ux, uy, spd, "drib"))

        best = None
        for ux, uy, spd, kind in cands:
            kk = kick_for(bvx, bvy, ux, uy, spd)
            if kk is None:
                continue
            kdir, power, speed = kk
            score = self.score_kick(bx, by, ux * speed, uy * speed, mates, opps, kid)
            if best is None or score > best[0]:
                best = (score, kdir, power, ux, uy)
        if best is None:
            return None
        _, kdir, power, ux, uy = best
        return kdir, power, (ux, uy)

    def score_kick(self, bx, by, vx, vy, mates, opps, kid):
        # Closed form along the straight line; stop at the boundary.
        k_goal = None
        pts = []
        lim_y = HALF_H - 0.45
        for k in SAMPLES:
            s = DT * (1.0 - FRIC ** k) / (1.0 - FRIC)
            x = bx + vx * s
            y = by + vy * s
            if x > HALF_W:
                # Where it crossed.
                if abs(y) < GOAL_HALF - 0.5:
                    k_goal = k
                break
            if x < -HALF_W + 0.5 or y > lim_y or y < -lim_y:
                break
            if x < -HALF_W + 2 and abs(y) < GOAL_HALF:
                return -5.0      # never into our own goal
            pts.append((k, x, y))
        if not pts:
            pts.append((0, bx, by))

        k_them = 999
        pt_them = pts[-1][1:]
        for ox, oy, ovx, ovy in opps:
            for k, x, y in pts:
                if k >= k_them:
                    break
                if reach_ticks(ox, oy, ovx, ovy, x, y) <= k + OPP_DELAY:
                    k_them = k
                    pt_them = (x, y)
                    break
        k_us = 999
        pt_us = pts[-1][1:]
        for mx, my, mvx, mvy, mid in mates:
            for k, x, y in pts:
                if k >= k_us:
                    break
                if mid == kid and k < 5:
                    continue
                if reach_ticks(mx, my, mvx, mvy, x, y) <= k:
                    k_us = k
                    pt_us = (x, y)
                    break

        if k_goal is not None and k_them > k_goal:
            p = _sig((min(k_them, 60) - k_goal) / 2.0 + 0.5)
            return 1.6 * p + (1 - p) * 0.0
        if k_us == 999 and k_them == 999:
            # Nobody gets there along the path: a race to where it stops.
            ex, ey = pts[-1][1], pts[-1][2]
            t_us = min(reach_ticks(m[0], m[1], m[2], m[3], ex, ey) for m in mates)
            t_th = min(reach_ticks(o[0], o[1], o[2], o[3], ex, ey) for o in opps)
            p = _sig((t_th - t_us) / 4.0)
            return p * value_ours(ex, ey) + (1 - p) * value_theirs(ex, ey)
        margin = k_them - k_us
        p = _sig((margin - 2.0) / 2.5)
        vo = value_ours(*pt_us) if k_us < 999 else 0.0
        vt = value_theirs(*pt_them)
        return p * vo + (1 - p) * vt


def cands_t(cands, pid):
    for t, i in cands:
        if i == pid:
            return t
    return 999


def travel_ticks(dist, speed):
    """Ticks for a ball launched at speed to roll dist (inf if it never does)."""
    step = speed * DT
    reach = step / (1.0 - FRIC)
    if dist >= reach * 0.98:
        return 200.0
    return math.log(1.0 - dist / reach) / math.log(FRIC)


def outside_circle(x, y, r):
    d = math.hypot(x, y)
    if d >= r or r <= 0:
        return x, y
    if d < 1e-6:
        return -r, 0.0
    return x / d * r, y / d * r


def steer_clear(p, mv, r):
    """Never step towards the centre spot while inside r + a safety band."""
    px, py = p.position
    vx, vy = p.velocity
    d = math.hypot(px, py)
    if d > r + 3.0 or d < 1e-6:
        return mv
    rx, ry = px / d, py / d
    mx, my = mv
    # Account for momentum: where will we be in a few ticks?
    inward = mx * rx + my * ry
    if inward < 0:
        mx -= inward * rx
        my -= inward * ry
    vin = vx * rx + vy * ry
    if d < r + 1.5 or vin < -2.0:
        mx += rx * 1.0
        my += ry * 1.0
    return (mx, my)


def arrive(p, tx, ty):
    """Full speed to a target, easing off at the end so we don't overshoot."""
    px, py = p.position
    dx, dy = tx - px, ty - py
    d = math.hypot(dx, dy)
    if d < 0.3:
        vx, vy = p.velocity
        return (-vx * 0.1, -vy * 0.1)
    thr = 1.0 if d > 2.0 else max(0.3, d / 2.0)
    return (dx / d * thr, dy / d * thr)


def assign(players, spots):
    """Minimum total distance assignment (players <= 4, so brute force)."""
    from itertools import permutations
    n = len(players)
    if n == 0:
        return {}
    best = None
    for perm in permutations(range(len(spots)), n):
        c = 0.0
        for i, j in enumerate(perm):
            px, py = players[i].position
            sx, sy = spots[j]
            c += math.hypot(px - sx, py - sy)
        if best is None or c < best[0]:
            best = (c, perm)
    return {players[i].id: spots[j] for i, j in enumerate(best[1])}
