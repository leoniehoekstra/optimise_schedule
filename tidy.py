import pandas as pd
from io import StringIO

# 1) Load your workshop schedule
schedule = pd.read_csv('workshop_schedule.csv', dtype=str)
valid_titles = set(schedule['Workshop Title'])


def load_clean_csv(path: str) -> pd.DataFrame:
    """Read slightly malformed CSV exported with all fields quoted."""
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip("\n").strip("\r")
            if line.startswith("\"") and line.endswith("\""):
                line = line[1:-1]
            line = line.replace("\"\"", "\"")
            lines.append(line)
    return pd.read_csv(StringIO("\n".join(lines)), dtype=str)

# 2) Load the student responses using the custom reader
answers = load_clean_csv('student_answers_v4.csv')

# — normalize all column names 💡
answers.columns = answers.columns.str.strip().str.lower()

print("Columns found:", answers.columns.tolist())

# 3) now refer to the exact (lowercase, stripped) names:
zb1_col = 'zonbrein_1'
zb2_col = 'zonbrein_2'
ex1_col = 'expressiebaai_1'
ex2_col = 'expressiebaai_2'
do1_col = 'doeeiland_1'
do2_col = 'doeeiland_2'



# 4) Prepare to build a “long” list of picks
records = []
warnings = []

for _, row in answers.iterrows():
    student = row['name']
    student_class = row['class']
    submitted_date = row['date']

    for zone, col, rank in [
        ('Zonbrein', zb1_col, 1),
        ('Zonbrein', zb2_col, 2),
        ('Expressiebaai', ex1_col, 1),
        ('Expressiebaai', ex2_col, 2),
        ('Doe-eiland', do1_col, 1),
        ('Doe-eiland', do2_col, 2),
    ]:
        # split the two choices and strip whitespace
        cell = row[col]
        if pd.isna(cell):
            continue
        picks = [w.strip() for w in str(cell).split(',')]
        for w in picks:
            if w not in valid_titles:
                warnings.append((student, zone, rank, w))
            else:
                records.append({
                    'Student': student,
                    'Class': student_class,
                    'Date': submitted_date,
                    'Zone': zone,
                    'Rank': rank,
                    'Workshop': w
                })

# 5) Turn into a DataFrame
prefs_long = pd.DataFrame.from_records(records)

# 6) (Optional) Show or save any invalid entries
if warnings:
    print("Found unmatched workshop titles:")
    for stu, zn, rk, w in warnings:
        print(f"  • {stu} ➔ {zn}, rank {rk}: “{w}”")

# 7) Save the long-form preferences
prefs_long.to_csv('student_preferences_long_v8.csv', index=False)

# 8) (Optional) Aggregate demand: count how many students gave each workshop each rank
demand = (
    prefs_long
    .groupby(['Zone', 'Rank', 'Workshop'])
    .size()
    .unstack(fill_value=0)
    .sort_index()
)

demand.to_csv('preference_counts_by_rank_v8.csv')

print("Done! 'student_preferences_long_v8.csv' and 'preference_counts_by_rank_v8.csv' generated.")