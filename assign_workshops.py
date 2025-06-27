#!/usr/bin/env python3
"""
assign_workshops.py

Assign students to workshops by minimizing sum of preference ranks,
respecting capacities and full-day exceptions, via PuLP.
"""

import pandas as pd
import pulp
from datetime import datetime

def load_data():
    sched = pd.read_csv(
        'workshop_schedule.csv',
        dtype={'Session': int, 'Full_Day_Session': int, 'Capacity': int}
    )
    prefs = pd.read_csv(
        'student_preferences_long_v8.csv',
        dtype={'Rank': int},
    )
    # parse submission dates so we can order students chronologically
    prefs['Parsed_Date'] = pd.to_datetime(
        prefs['Date'], format='%d-%m-%Y %H:%M:%S'
    )
    return sched, prefs

def build_zone_map(prefs):
    return prefs.groupby('Workshop')['Zone'].first().to_dict()

def build_costs(prefs):
    cost = {}
    for r in prefs.itertuples(index=False):
        key = (r.Student, r.Workshop)
        cost[key] = min(r.Rank, cost.get(key, r.Rank))
    return cost

def build_student_dates(prefs):
    """Return a mapping from student name to submission datetime."""
    return prefs.groupby('Student')['Parsed_Date'].first().to_dict()


def greedy_assign(students, zone_map, cost, cap_map, full_map, days):
    """Greedily assign remaining students to any available slots.

    The assignment prefers lower ranked workshops but does not enforce the
    complex two-per-zone requirement.  Capacity limits and the restriction
    that a student may not receive the same workshop twice are still
    honoured.
    """

    rows = []
    workshops = sorted({w for (w, _, _) in cap_map})

    for s in students:
        used_workshops = set()
        used_slots = {}

        for day in days:
            # try a full-day workshop first
            full_candidates = [
                w for (w, d, t) in cap_map
                if d == day and t == 0 and cap_map[(w, d, t)] > 0
                and w not in used_workshops
            ]
            full_candidates.sort(key=lambda w: cost.get((s, w), 99))

            if full_candidates:
                w = full_candidates[0]
                rows.append({
                    'Student': s,
                    'Zone': zone_map[w],
                    'Day': day,
                    'Session': 0,
                    'Workshop Title': w,
                })
                cap_map[(w, day, 0)] -= 1
                used_workshops.add(w)
                used_slots[(day, 1)] = True
                used_slots[(day, 2)] = True
                continue

            # otherwise handle the two half-day slots
            for sess in [1, 2]:
                if used_slots.get((day, sess)):
                    continue

                candidates = [
                    w for (w, d, t) in cap_map
                    if d == day and t == sess and cap_map[(w, d, t)] > 0
                    and w not in used_workshops
                ]
                if not candidates:
                    continue

                candidates.sort(key=lambda w: cost.get((s, w), 99))
                w = candidates[0]
                rows.append({
                    'Student': s,
                    'Zone': zone_map[w],
                    'Day': day,
                    'Session': sess,
                    'Workshop Title': w,
                })
                cap_map[(w, day, sess)] -= 1
                used_workshops.add(w)
                used_slots[(day, sess)] = True

    return rows


def solve_group(students, zone_map, cost, cap_map, full_map, days, *, late: bool = False):
    """Assign workshops for a subset of students.

    Parameters
    ----------
    students : list[str]
        Students to schedule in this run.
    zone_map : dict[str, str]
        Mapping from workshop to zone.
    cost : dict[tuple[str, str], int]
        Mapping (student, workshop) -> rank cost.
    cap_map : dict[tuple[str, str, int], int]
        Remaining capacity for each (workshop, day, session).
    full_map : dict[tuple[str, str, int], bool]
        Whether the slot is a full day session.
    days : list[str]
        Ordered list of days in the schedule.

    Returns
    -------
    list[dict]
        Rows describing the assignments for the provided students.  The
        ``cap_map`` will be updated in place.
    """

    if not students:
        return []

    # ``late`` students will be handled by a greedy heuristic instead of
    # solving the full MILP.  This keeps the problem feasible while still
    # respecting capacities and avoiding duplicate workshops.
    if late:
        return greedy_assign(students, zone_map, cost, cap_map, full_map, days)

    prob = pulp.LpProblem('Workshop_Assignment', pulp.LpMinimize)
    x = {
        (s, w, d, t): pulp.LpVariable(
            f"x_{s}_{w.replace(' ','_')}_{d}_T{t}", cat='Binary'
        )
        for s in students
        for (w, d, t) in cap_map
    }

    zones = sorted(set(zone_map.values()))

    # ------------------------------------------------------------------
    # Two per zone with first→second→random fallback
    # ------------------------------------------------------------------
    for s in students:
        for z in zones:
            half_pairs = [
                (w, x[(s,w,d,t)])
                for (w,d,t) in cap_map
                if not full_map[(w,d,t)] and zone_map[w] == z
            ]
            full_pairs = [
                (w, x[(s,w,d,0)])
                for (w,d,t) in cap_map
                if     full_map[(w,d,t)] and zone_map[w] == z
            ]

            # collect distinct first‑ and second‑choice **workshop titles**
            rank1_set = { w for (w,_) in half_pairs+full_pairs
                        if cost.get((s,w),999)==1 }
            rank2_set = { w for (w,_) in half_pairs+full_pairs
                        if cost.get((s,w),999)==2 and w not in rank1_set }

            # bucket the vars by preference tier
            first_half  = [v for (w,v) in half_pairs if w in rank1_set]
            first_full  = [v for (w,v) in full_pairs if w in rank1_set]
            second_half = [v for (w,v) in half_pairs if w in rank2_set]
            second_full = [v for (w,v) in full_pairs if w in rank2_set]
            other_half  = [v for (w,v) in half_pairs
                            if w not in rank1_set and w not in rank2_set]
            other_full  = [v for (w,v) in full_pairs
                            if w not in rank1_set and w not in rank2_set]

            fsz = pulp.lpSum(first_half)  + 2*pulp.lpSum(first_full)
            ssz = pulp.lpSum(second_half) + 2*pulp.lpSum(second_full)
            osz = pulp.lpSum(other_half)  + 2*pulp.lpSum(other_full)

            # how many random slots are allowed?
            # only if no distinct second choices were provided
            allow_random = 1 if len(rank2_set)==0 else 0

            # 1) exactly two slots in this zone
            prob += (fsz + ssz + osz == 2,
                    f"TwoPerZone_{s}_{z}")

            # 2) fill any missing #1 slots with #2s
            prob += (ssz >= 2 - fsz,
                    f"UseSeconds_{s}_{z}")

            # 3) if they supplied *no* distinct 2nd choices, allow up to one wild‑card
            prob += (osz <= allow_random,
                    f"RandLimit_{s}_{z}")
    # ------------------------------------------------------------------

    # Capacity constraints
    for (w, d, t), cap in cap_map.items():
        prob += (
            pulp.lpSum(x[(s, w, d, t)] for s in students) <= cap,
            f"Cap_{w.replace(' ','_')}_{d}_T{t}"
        )

    # # ── No repeats: each student may take a given workshop at most once ──
    # workshops = sorted({ w for (w, d, t) in cap_map.keys() })
    # for s in students:
    #     for w0 in workshops:
    #         # collect all decision vars for this student & workshop
    #         terms = []
    #         for (s2, w2, d2, t2), var in x.items():
    #             if s2 == s and w2 == w0:
    #                 terms.append(var)
    #         # enforce at most one assignment to workshop w0 for student s
    #         prob += (
    #             pulp.lpSum(terms) <= 1,
    #             f"NoRepeat_{s}_{w0.replace(' ','_')}"
    #         )
    #     # ── No repeats: each student may take a given workshop at most once ──
        # ── No repeats: each student may take a given workshop at most once ──
    workshops = sorted({ w for (w, d, t) in cap_map.keys() })
    for s in students:
        for w in workshops:
            prob += (
                pulp.lpSum(
                    x[(s, w, d, t)]
                    for (w2, d, t) in cap_map
                    if w2 == w
                ) <= 1,
                f"NoRepeat_{s}_{w.replace(' ','_')}"
            )




    # One slot per day/session
    for s in students:
        for d in days:
            prob += (
                pulp.lpSum(
                    x[(s, w, d, 1)]
                    for (w, d2, t) in cap_map
                    if d2 == d and t == 1
                )
                + pulp.lpSum(
                    x[(s, w, d, 0)]
                    for (w, d2, t) in cap_map
                    if d2 == d and t == 0 and full_map[(w, d2, t)]
                )
                == 1,
                f"OnePerSlot_{s}_{d}_Sess1",
            )
            prob += (
                pulp.lpSum(
                    x[(s, w, d, 2)]
                    for (w, d2, t) in cap_map
                    if d2 == d and t == 2
                )
                + pulp.lpSum(
                    x[(s, w, d, 0)]
                    for (w, d2, t) in cap_map
                    if d2 == d and t == 0 and full_map[(w, d2, t)]
                )
                == 1,
                f"OnePerSlot_{s}_{d}_Sess2",
            )

    # Objective
    prob += pulp.lpSum(
        cost.get((s, w), 99) * var
        for ((s, w, d, t), var) in x.items()
    )


    result = prob.solve(pulp.PULP_CBC_CMD(msg=True, timeLimit=60))
    status = pulp.LpStatus[prob.status]
    if status != 'Optimal':
        print(f"MILP solver returned {status}. Falling back to greedy.")
        return greedy_assign(students, zone_map, cost, cap_map, full_map, days)

    rows = []
    for (s, w, d, t), var in x.items():
        if var.value() == 1:
            rows.append({
                'Student': s,
                'Zone': zone_map[w],
                'Day': d,
                'Session': t,
                'Workshop Title': w,
            })
            cap_map[(w, d, t)] -= 1

    return rows

def main():
    # 1) Load everything
    sched, prefs = load_data()
    zone_map = build_zone_map(prefs)
    cost = build_costs(prefs)
    dates = build_student_dates(prefs)

    # 2) Define exactly-preassigned slots for Jesse & Niels
    pre_assign = {
        'jesse wolters': [
            ('Vissen', 'Tuesday', 1),
            ('Papier leer', 'Tuesday', 2),
            ('Hond in de hoofdrol', 'Wednesday', 1),
            ('Vuvuzela maken', 'Wednesday', 2),
            ('Naar de kaasboerderij', 'Thursday', 0),
        ],
        'niels hielkema': [
            ('Vissen', 'Tuesday', 1),
            ('Papier leer', 'Tuesday', 2),
            ('Hond in de hoofdrol', 'Wednesday', 1),
            ('Vuvuzela maken', 'Wednesday', 2),
            ('Naar de kaasboerderij', 'Thursday', 0),
        ],
    }

    forced_students = set(pre_assign.keys())

    # 3) Build capacity maps and subtract preassigned seats
    grp = (
        sched
        .groupby(['Day', 'Workshop Title', 'Session', 'Full_Day_Session'])['Capacity']
        .sum()
        .reset_index()
    )

    days = sorted(grp['Day'].unique())
    cap_map = {}
    full_map = {}
    for _, row in grp.iterrows():
        w = row['Workshop Title']
        t = int(row['Session'])
        d = row['Day']
        full = bool(int(row['Full_Day_Session']))
        cap = int(row['Capacity'])
        for student, slots in pre_assign.items():
            if (w, d, t) in slots:
                cap -= 1
        cap_map[(w, d, t)] = cap
        full_map[(w, d, t)] = full

    # 4) Determine student groups based on submission date
    early_cut = datetime(2025, 6, 23)
    mid_cut = datetime(2025, 6, 24)

    all_students = [s for s in sorted(prefs['Student'].unique()) if s not in forced_students]
    students_early = [s for s in all_students if dates[s] < early_cut]
    students_mid = [s for s in all_students if early_cut <= dates[s] < mid_cut]
    students_late = [s for s in all_students if dates[s] >= mid_cut]

    rows = []
    for student, slots in pre_assign.items():
        for (w, d, t) in slots:
            rows.append({
                'Student': student,
                'Zone': zone_map[w],
                'Day': d,
                'Session': t,
                'Workshop Title': w,
            })

    # 5) Solve sequentially for each group
    rows += solve_group(students_early, zone_map, cost, cap_map, full_map, days, late=False)

    # Remaining students are scheduled greedily based on their preferences
    remaining_students = students_mid + students_late
    rows += solve_group(remaining_students, zone_map, cost, cap_map, full_map, days, late=True)

    out = pd.DataFrame(rows)
    out = out[['Student', 'Zone', 'Day', 'Session', 'Workshop Title']]

    dups = (
        out
        .groupby(['Student', 'Workshop Title'])
        .size()
        .reset_index(name='count')
        .query('count > 1')
    )
    if not dups.empty:
        print('Found duplicate assignments!')
        print(dups)

    out.to_csv('FINAL_workshop_schedule_v1.csv', index=False)

    print('Solved sequentially. Assignments saved to FINAL_workshop_schedule_v1.csv')


if __name__ == '__main__':
    main()
