# elective-scout

You open Quest to add an elective and hit a wall:

- Prerequisite not met — rejected.
- Swap to another one, time conflict with your existing courses — rejected.
- Try a third, it's at the Cambridge or St. Jerome's campus — not worth the commute.
- Try a fourth, it's fully online, not what you wanted.

Each attempt means checking the calendar for prereqs, then manually hunting through class sections on classes.uwaterloo.ca, then cross-referencing your timetable. For a long elective list this can take an entire afternoon.

This tool does all three things in one shot: **which electives you already meet the prereqs for, where they're offered this term, and which ones don't conflict with your registered courses**. You get a filtered list you can act on directly.

---

## Usage

```bash
python elective_scout.py
```

The script will ask you:

1. **Your program** — type a keyword (e.g. `electrical`, `software`, `computer eng`) and pick from the matched list
2. **Your current academic standing** (e.g. `3A`)
3. **Electives you completed in past terms** (Enter to skip if none)
4. **Any extra courses registered this term beyond your required ones** (Enter to skip)
5. **Schedule term** (e.g. `W26`, `S26`, `F26`; Enter for the current term)

Required courses for your current standing are **auto-detected** from the program catalog — you don't need to enter them manually.

**Default output** — prints eligible course codes directly to the terminal:

```
Eligible electives — spring2026

online (3):
  ANTH101  CS449  PSYCH100

no conflict (9):
  ECE302  ECE405  ECE414  ECE488  ECE493  MTE546  SYDE522  SYDE556  STAT441
```

Add `--verbose` to also show course titles. Add `--report` to write the full classification files to disk.

---

## How it works

**Step 1 — Prereq classification**

Pulls your program's elective list from the Kuali catalog and walks the prerequisite tree for every course to determine the earliest term you can take it: `2B / 3A / 3B / impossible`.

The `impossible` judgment is personalized. Courses whose prereqs are satisfied by your standing are classified correctly — required courses from earlier terms are automatically marked as completed. Past electives you provide are used to resolve cases where one elective is a prereq for another.

**Step 2 — Schedule lookup**

For every non-impossible course, queries the schedule for the chosen term and groups by location:

- `online` — all sections are online
- `UW U` — has in-person sections, all at the UW main campus
- `other` — has in-person sections, but not all at UW main campus
- `n/a` — no sections found this term

**Step 3 — Conflict check**

For `UW U` courses, the required courses for your current standing are auto-fetched to build your occupied time blocks. Each elective is then checked against those blocks and marked `no conflict` or `with conflict`.

---

## Input format for registered courses

Comma- or semicolon-separated, all on one line:

```
MATH239, ECE250 LEC 001, ECE316:LEC 001
```

Writing just a course code (e.g. `MATH239`) is conservative — all sections are treated as occupied. Writing a specific section limits the check to that section only.

---

## Scripted / non-interactive use

All prompts can be bypassed with flags:

| flag | description |
|---|---|
| `--non-interactive` | disable all prompts; driven entirely by flags |
| `--catalog-id` | Kuali catalog ID |
| `--program-pid` | program PID |
| `--program-name` | full program name |
| `--schedule-term` | term alias or code (e.g. `W26`, `S26`, `1265`) |
| `--output-dir` | output directory (default: current directory) |
| `--output-prefix` | filename prefix (default: derived from program name) |
| `--report` | write classification and schedule files to disk |
| `--verbose` | show course titles in terminal output |
| `--student-major-name` | student's program name |
| `--student-standing` | academic standing e.g. `3A` |
| `--student-completed-course` (repeatable) | past completed elective |
| `--student-registered-course` (repeatable) | extra registered course this term |
| `--allowed-marker` (repeatable) | additional prereq text fragment to treat as satisfied |
