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


def greedy_assign(
    students,
    zone_map,
    cost,
    cap_map,
    full_map,
    days,
    *,
    pre_half=None,
    pre_full=None,
    pre_slots=None,
):
    """Greedily assign remaining students while respecting zone quotas.

    This heuristic attempts to satisfy the rule that every student follows
    the "two workshops per zone" guideline.  A full‑day workshop counts as
    two.  Previously this function simply grabbed any available workshop and
    therefore violated the zone requirement for many students.

    Parameters
    ----------
    pre_half, pre_full : dict
        Pre-assigned half-/full-day counts per ``(student, zone)``.
    pre_slots : dict
        Mapping ``(student, day, session)`` to the number of already filled
        slots.  This prevents double-booking in slots that were fixed in
        advance.
    """

    pre_half = pre_half or {}
    pre_full = pre_full or {}
    pre_slots = pre_slots or {}

    rows = []
    workshops = sorted({w for (w, _, _) in cap_map})
    zones = sorted(set(zone_map.values()))

    for s in students:
        used_workshops = set()
        used_slots = {
            (d, t): True
            for (stu, d, t), val in pre_slots.items()
            if stu == s and val
        }

        zone_need = {
            z: max(0, 2 - pre_half.get((s, z), 0) - 2 * pre_full.get((s, z), 0))
            for z in zones
        }
        zone_full_used = {z: pre_full.get((s, z), 0) for z in zones}

        for day in days:
            # full-day assignment if both sessions free and a zone still needs 2
            if not used_slots.get((day, 1)) and not used_slots.get((day, 2)):
                full_candidates = [
                    w
                    for (w, d, t) in cap_map
                    if d == day
                    and t == 0
                    and cap_map[(w, d, t)] > 0
                    and zone_need[zone_map[w]] == 2
                    and zone_full_used[zone_map[w]] == 0
                    and w not in used_workshops
                ]
                if full_candidates:
                    w = min(full_candidates, key=lambda w: cost.get((s, w), 99))
                    z = zone_map[w]
                    rows.append(
                        {
                            'Student': s,
                            'Zone': z,
                            'Day': day,
                            'Session': 0,
                            'Workshop Title': w,
                        }
                    )
                    cap_map[(w, day, 0)] -= 1
                    used_workshops.add(w)
                    used_slots[(day, 1)] = True
                    used_slots[(day, 2)] = True
                    zone_need[z] = 0
                    zone_full_used[z] = 1
                    continue

            # handle half-day slots
            for sess in [1, 2]:
                if used_slots.get((day, sess)):
                    continue

                candidates = [
                    w
                    for (w, d, t) in cap_map
                    if d == day
                    and t == sess
                    and cap_map[(w, d, t)] > 0
                    and w not in used_workshops
                    and zone_need[zone_map[w]] > 0
                ]

                if not candidates:
                    # no candidate that fits zone quota, try any zone
                    candidates = [
                        w
                        for (w, d, t) in cap_map
                        if d == day and t == sess and cap_map[(w, d, t)] > 0
                        and w not in used_workshops
                    ]
                    if not candidates:
                        continue

                w = min(candidates, key=lambda w: cost.get((s, w), 99))
                z = zone_map[w]
                rows.append(
                    {
                        'Student': s,
                        'Zone': z,
                        'Day': day,
                        'Session': sess,
                        'Workshop Title': w,
                    }
                )
                cap_map[(w, day, sess)] -= 1
                used_workshops.add(w)
                used_slots[(day, sess)] = True
                zone_need[z] = max(0, zone_need[z] - 1)

    return rows


from collections import defaultdict


def solve_group(
    students,
    zone_map,
    cost,
    cap_map,
    full_map,
    days,
    *,
    pre_half=None,
    pre_full=None,
    pre_slots=None,
    late: bool = False,
):
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
        return greedy_assign(
            students,
            zone_map,
            cost,
            cap_map,
            full_map,
            days,
            pre_half=pre_half,
            pre_full=pre_full,
            pre_slots=pre_slots,
        )

    pre_half = pre_half or {}
    pre_full = pre_full or {}
    pre_slots = pre_slots or {}

    def build_problem(enforce_zone=True):
        prob = pulp.LpProblem('Workshop_Assignment', pulp.LpMinimize)
        x = {
            (s, w, d, t): pulp.LpVariable(
                f"x_{s}_{w.replace(' ','_')}_{d}_T{t}", cat='Binary'
            )
            for s in students
            for (w, d, t) in cap_map
        }
    
        zones = sorted(set(zone_map.values()))
    
        if enforce_zone:
            for s in students:
                for z in zones:
                    pre_h = pre_half.get((s, z), 0)
                    pre_f = pre_full.get((s, z), 0)
                    half_pairs = [
                        (w, x[(s, w, d, t)])
                        for (w, d, t) in cap_map
                        if not full_map[(w, d, t)] and zone_map[w] == z
                    ]
                    full_pairs = [
                        (w, x[(s, w, d, 0)])
                        for (w, d, t) in cap_map
                        if full_map[(w, d, t)] and zone_map[w] == z
                    ]
    
                    rank1_set = {w for (w, _) in half_pairs + full_pairs if cost.get((s, w), 999) == 1}
                    rank2_set = {w for (w, _) in half_pairs + full_pairs if cost.get((s, w), 999) == 2 and w not in rank1_set}
    
                    first_half = [v for (w, v) in half_pairs if w in rank1_set]
                    first_full = [v for (w, v) in full_pairs if w in rank1_set]
                    second_half = [v for (w, v) in half_pairs if w in rank2_set]
                    second_full = [v for (w, v) in full_pairs if w in rank2_set]
                    other_half = [v for (w, v) in half_pairs if w not in rank1_set and w not in rank2_set]
                    other_full = [v for (w, v) in full_pairs if w not in rank1_set and w not in rank2_set]

                    first_total = pulp.lpSum(first_half) + pulp.lpSum(first_full)
                    second_total = pulp.lpSum(second_half) + pulp.lpSum(second_full)
                    other_total = pulp.lpSum(other_half) + pulp.lpSum(other_full)
                    full_total = pulp.lpSum(first_full + second_full + other_full)

                    # Only allow a random workshop when no second-preference
                    # options exist for this student in this zone.  This keeps
                    # the zone constraint strict while still letting the model
                    # fill otherwise empty slots.
                    allow_random = 1 if len(rank2_set) == 0 else 0

                    required = 2 - pre_h - 2 * pre_f

                    # ``first_total + second_total + other_total`` counts every
                    # assigned workshop once, regardless of duration.  We want
                    # a full‑day workshop to count as two half‑day slots, so
                    # add ``full_total`` once more instead of twice.  The
                    # previous version mistakenly weighted a full‑day workshop
                    # by three which made the strict MILP infeasible whenever a
                    # full‑day slot was chosen.
                    prob += (
                        first_total + second_total + other_total + full_total
                        == required,
                        f"TwoPerZone_{s}_{z}",
                    )
                    prob += (
                        second_total >= (required - 2 * full_total) - first_total,
                        f"UseSeconds_{s}_{z}",
                    )
                    prob += (
                        other_total <= allow_random,
                        f"RandLimit_{s}_{z}",
                    )
                    prob += (full_total <= 1 - pre_f, f"OneFull_{s}_{z}")
    
        for (w, d, t), cap in cap_map.items():
            prob += (
                pulp.lpSum(x[(s, w, d, t)] for s in students) <= cap,
                f"Cap_{w.replace(' ','_')}_{d}_T{t}"
            )
    
        workshops = sorted({w for (w, d, t) in cap_map.keys()})
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
    
        for s in students:
            for d in days:
                pre1 = pre_slots.get((s, d, 1), 0)
                pre2 = pre_slots.get((s, d, 2), 0)
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
                    == 1 - pre1,
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
                    == 1 - pre2,
                    f"OnePerSlot_{s}_{d}_Sess2",
                )
    
        prob += pulp.lpSum(
            cost.get((s, w), 99) * var
            for ((s, w, d, t), var) in x.items()
        )
    
        return prob, x
    
    prob, x = build_problem(enforce_zone=True)
    prob.solve(pulp.PULP_CBC_CMD(msg=True, timeLimit=60))
    status = pulp.LpStatus[prob.status]
    
    if status != 'Optimal':
        print('Strict MILP infeasible; relaxing zone constraints')
        prob, x = build_problem(enforce_zone=False)
        prob.solve(pulp.PULP_CBC_CMD(msg=True, timeLimit=60))
        status = pulp.LpStatus[prob.status]
    
    if status != 'Optimal':
        print(f"MILP solver returned {status}. Falling back to greedy.")
        return greedy_assign(
            students,
            zone_map,
            cost,
            cap_map,
            full_map,
            days,
            pre_half=pre_half,
            pre_full=pre_full,
            pre_slots=pre_slots,
        )
    
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

    def load_previous_assignments(titles):
        """Return mapping of students to already assigned workshops.

        Parameters
        ----------
        titles : Iterable[str]
            Workshop titles to consider.  Matching is case-insensitive.
        """

        df = pd.read_csv('einde.csv')
        titles_lower = {t.lower() for t in titles}
        df = df[df['Workshop Title'].str.lower().isin(titles_lower)]

        mapping = {}
        for _, r in df.iterrows():
            stu = r['Student']
            w = r['Workshop Title']
            d = r['Day']
            t = int(r['Session'])
            mapping.setdefault(stu, []).append((w, d, t))
        return mapping

    # merge all preassigned slots from earlier runs
    prev_titles = ['Vissen', 'Camping sportactiviteiten', 'Eigen sportspel ontwerpen']
    prev_assignments = load_previous_assignments(prev_titles)
    for stu, slots in prev_assignments.items():
        if stu in pre_assign:
            existing = set(pre_assign[stu])
            for sl in slots:
                if sl not in existing:
                    pre_assign[stu].append(sl)
        else:
            pre_assign[stu] = slots

    forced_students = {'jesse wolters', 'niels hielkema'} | set(prev_assignments)

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

    # count pre-assigned slots per zone and per day/session
    pre_half = defaultdict(int)
    pre_full = defaultdict(int)
    pre_slots = defaultdict(int)
    for student, slots in pre_assign.items():
        for (w, d, t) in slots:
            z = zone_map[w]
            if t == 0:
                pre_full[(student, z)] += 1
                pre_slots[(student, d, 1)] += 1
                pre_slots[(student, d, 2)] += 1
            else:
                pre_half[(student, z)] += 1
                pre_slots[(student, d, t)] += 1

    # 4) Determine student groups based on submission date
    early_cut = datetime(2025, 6, 23)
    mid_cut = datetime(2025, 6, 24)

    all_students = sorted(prefs['Student'].unique())
    all_students = [s for s in all_students if s not in forced_students]
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
    rows += solve_group(
        students_early,
        zone_map,
        cost,
        cap_map,
        full_map,
        days,
        pre_half=pre_half,
        pre_full=pre_full,
        pre_slots=pre_slots,
        late=False,
    )
    rows += solve_group(
        students_mid,
        zone_map,
        cost,
        cap_map,
        full_map,
        days,
        pre_half=pre_half,
        pre_full=pre_full,
        pre_slots=pre_slots,
        late=False,
    )
    rows += solve_group(
        students_late,
        zone_map,
        cost,
        cap_map,
        full_map,
        days,
        pre_half=pre_half,
        pre_full=pre_full,
        pre_slots=pre_slots,
        late=True,
    )

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
