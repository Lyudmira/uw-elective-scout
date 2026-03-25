#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import traceback
import ssl
import urllib.parse
import urllib.request
from urllib.parse import quote
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from html.parser import HTMLParser as _HTMLParser


BASE_URL = "https://uwaterloocm.kuali.co/api/v1/catalog"
CATALOG_PAGE_URL = "https://uwaterloo.ca/academic-calendar/undergraduate-studies/catalog"
UWFLOW_GRAPHQL_URL = "https://uwflow.com/graphql"
SCHEDULE_BASE_URL = "https://classes.uwaterloo.ca/cgi-bin/cgiwrap/infocour/salook.pl"
TERM_ORDER = {"1A": 1, "1B": 2, "2A": 3, "2B": 4, "3A": 5, "3B": 6, "4A": 7, "4B": 8}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}
DEBUG_REQUESTS = True


def _debug_print(message: str) -> None:
    if DEBUG_REQUESTS:
        print(f"[debug] {message}", file=sys.stderr)


def _debug_exception(label: str, error: BaseException) -> None:
    if not DEBUG_REQUESTS:
        return
    _debug_print(f"{label}: {type(error).__name__}: {error}")
    tb = traceback.format_exc().rstrip()
    if tb and tb != "NoneType: None":
        print(tb, file=sys.stderr)


def build_ssl_context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


SSL_CONTEXT = build_ssl_context()


@dataclass(frozen=True)
class ScheduleSectionRow:
    class_num: str
    component: str
    camp_loc: str
    assoc_class: str
    rel1: str
    rel2: str
    enrl_cap: str
    enrl_tot: str
    wait_cap: str
    wait_tot: str
    time_days_date: str
    bldg_room: str
    instructor: str

    @property
    def is_online(self) -> bool:
        compact = " ".join([self.camp_loc, self.bldg_room, self.time_days_date, self.instructor]).upper()
        return "ONLN" in compact or "ONLINE" in compact


@dataclass(frozen=True)
class CourseRecord:
    group: str
    dbid: str
    code: str
    title: str
    prereq_text: str
    rank: int
    category: str


@dataclass(frozen=True)
class ScheduleRecord:
    group: str
    code: str
    title: str
    category: str
    campus_group: str
    status: str
    term: str
    section_lines: tuple[str, ...]
    section_rows: tuple[dict[str, str | bool], ...]


@dataclass(frozen=True)
class RegisteredCourseEntry:
    raw: str
    code: str
    component: str | None
    class_num: str | None


@dataclass(frozen=True)
class StudentProfile:
    major_name: str
    standing: str
    completed_elective_entries: tuple[RegisteredCourseEntry, ...]  # past semesters, used for prereq resolution
    registered_entries: tuple[RegisteredCourseEntry, ...]  # current semester, used for timetable conflict


@dataclass(frozen=True)
class MeetingBlock:
    days: tuple[str, ...]
    start_minute: int
    end_minute: int
    label: str


@dataclass(frozen=True)
class UWFlowStats:
    easy: int | None
    useful: int | None


def schedule_parse_course(value: str) -> tuple[str, str]:
    cleaned = value.strip().upper().replace(" ", "")
    match = re.fullmatch(r"([A-Z]{2,5})(\d{2,4}[A-Z]?)", cleaned)
    if not match:
        raise ValueError(f"invalid course code: {value!r}")
    return match.group(1), match.group(2)


def schedule_default_term() -> str:
    today = dt.date.today()
    if today < dt.date(today.year, 4, 1):
        return f"1{today.year % 100:02d}1"
    if today < dt.date(today.year, 9, 1):
        return f"1{today.year % 100:02d}5"
    return f"1{today.year % 100:02d}9"


def schedule_fetch_schedule(level: str, sess: str, subject: str, cournum: str) -> str:
    payload = urllib.parse.urlencode(
        {
            "level": level,
            "sess": sess,
            "subject": subject,
            "cournum": cournum,
        }
    ).encode("utf-8")
    request = urllib.request.Request(SCHEDULE_BASE_URL, data=payload, headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
        return response.read().decode("utf-8", "ignore")


def schedule_extract_no_matches(html_text: str) -> bool:
    return "Sorry, but your query had no matches." in html_text


def schedule_strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def schedule_extract_section_table(html_text: str) -> tuple[str, list[ScheduleSectionRow]]:
    title_match = re.search(
        r"Your selection was:<BR>\s*Level:\s*(.*?)\s*,\s*Term:\s*(\d+)\s*,\s*Subject:\s*(.*?)\s*,\s*Course Number:\s*(.*?)<P>",
        html_text,
        re.S | re.I,
    )
    header = ""
    if title_match:
        header = f"{schedule_strip_tags(title_match.group(3))} {schedule_strip_tags(title_match.group(4))} (term {title_match.group(2)})"

    rows: list[ScheduleSectionRow] = []
    table_match = re.search(r"<TABLE BORDER=2>(.*)</TABLE>\s*</TD></TR>\s*<TR><TD COLSPAN = 4></TD></TR>\s*</TABLE>", html_text, re.S | re.I)
    if not table_match:
        return header, rows

    table_html = table_match.group(1)
    row_matches = re.findall(r"<TR>(.*?)</TR>", table_html, re.S | re.I)
    for raw_row in row_matches:
        if "<TH>" in raw_row:
            continue
        if "COLSPAN=6" in raw_row.upper() or "COLSPAN = 6" in raw_row.upper():
            continue

        cells = re.findall(r"<TD[^>]*>(.*?)</TD>", raw_row, re.S | re.I)
        if len(cells) < 12:
            continue

        values = [schedule_strip_tags(cell).replace("\xa0", " ") for cell in cells[:13]]
        values = [re.sub(r"\s+", " ", value).strip() for value in values]

        while len(values) < 13:
            values.append("")

        if not values[0] and not values[1]:
            continue

        rows.append(
            ScheduleSectionRow(
                class_num=values[0],
                component=values[1],
                camp_loc=values[2],
                assoc_class=values[3],
                rel1=values[4],
                rel2=values[5],
                enrl_cap=values[6],
                enrl_tot=values[7],
                wait_cap=values[8],
                wait_tot=values[9],
                time_days_date=values[10],
                bldg_room=values[11],
                instructor=values[12],
            )
        )

    return header, rows


def fetch_json(url: str) -> dict | list:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            _debug_print(f"GET {url}")
            request = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception as error:  # pragma: no cover - network retry path
            _debug_exception(f"GET failed for {url}", error)
            last_error = error
    assert last_error is not None
    raise last_error


def post_json(url: str, payload: dict) -> dict | list:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            _debug_print(f"POST {url}")
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={**HTTP_HEADERS, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception as error:  # pragma: no cover - network retry path
            _debug_exception(f"POST failed for {url}", error)
            last_error = error
    assert last_error is not None
    raise last_error


def fetch_uwflow_stats(code: str) -> UWFlowStats:
    try:
        response = post_json(
            UWFLOW_GRAPHQL_URL,
            {
                "query": "query CourseStats($code: String!) { course_search_index(where: {code: {_eq: $code}}) { easy useful } }",
                "variables": {"code": code.lower().replace(" ", "")},
            },
        )
    except Exception:
        return UWFlowStats(easy=None, useful=None)

    rows = response.get("data", {}).get("course_search_index", []) if isinstance(response, dict) else []
    if not rows:
        return UWFlowStats(easy=None, useful=None)

    row = rows[0]

    def to_percent(value: object) -> int | None:
        if not isinstance(value, (int, float)):
            return None
        return int(round(float(value) * 100))

    return UWFlowStats(
        easy=to_percent(row.get("easy")),
        useful=to_percent(row.get("useful")),
    )


def format_uwflow_stats(stats: UWFlowStats) -> str:
    easy = "n/a" if stats.easy is None else f"{stats.easy}%"
    useful = "n/a" if stats.useful is None else f"{stats.useful}%"
    return f"easy {easy} | useful {useful}"


def resolve_catalog_id() -> str:
    request = urllib.request.Request(CATALOG_PAGE_URL, headers=HTTP_HEADERS)
    page = urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT).read().decode("utf-8", "ignore")
    match = re.search(r"catalogId[\"']?\s*[:=]\s*[\"']([^\"']+)", page, re.I)
    if not match:
        raise RuntimeError("Could not determine the active Kuali catalog id from the Waterloo academic calendar page.")
    return match.group(1)


def build_allowed_markers(program_name: str) -> list[str]:
    markers = {
        program_name,
        re.sub(r"\s*\([^)]*\)", "", program_name).strip(),
        "co-operative program",
        "BASc program",
    }

    if "Engineering" in program_name:
        markers.update(
            {
                "Faculty of Engineering",
                "Faculties of Engineering, Mathematics, or Science",
                "Engineering program",
                "program offered by the Faculty of Engineering",
                "program offered by Faculty of Engineering",
                "program offered by the Faculties of Engineering, Mathematics, or Science",
                "program offered by an engineering faculty",
                "program offered by the Faculties of Engineering",
            }
        )
    if re.search(r"Bachelor of Science|Honours Science|Faculty of Science", program_name):
        markers.add("Honours Science program")

    return sorted(marker for marker in markers if marker)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def term_rank_from_code(code: str, required_rank_by_code: dict[str, int]) -> int:
    if code in required_rank_by_code:
        return required_rank_by_code[code]
    if code in {"PD19", "PD20", "PD22"}:
        return 0
    return 99


def build_required_rank_map(program: dict) -> dict[str, int]:
    required_rank_by_code: dict[str, int] = {}
    for section_html in re.findall(r"<section>(.*?)</section>", program["requiredCoursesTermByTerm"]):
        title_match = re.search(r"<h2[^>]*><span>(.*?)</span></h2>", section_html)
        if not title_match:
            continue
        term_name = html.unescape(title_match.group(1)).split()[0]
        rank = TERM_ORDER.get(term_name)
        if rank is None:
            continue
        for code in re.findall(r'<a href="#/courses/view/[^\"]+" target="_blank">([^<]+)</a>', section_html):
            required_rank_by_code[code] = rank
    return required_rank_by_code


def extract_course_list_items(program: dict) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    sections = re.findall(r"<section>(.*?)</section>", program["courseListsNew"])
    for section_html in sections:
        title_match = re.search(r"<h2[^>]*>(?:<span>)?(.*?)(?:</span>)?</h2>", section_html)
        if title_match:
            group = normalize_text(re.sub(r"<[^>]+>", "", title_match.group(1)))
        else:
            group = "Unknown"
        if not group:
            header_match = re.search(r"<h2>(.*?)</h2>", section_html)
            group = normalize_text(re.sub(r"<[^>]+>", "", header_match.group(1))) if header_match else "Unknown"

        for dbid, code, title in re.findall(
            r'<a href="#/courses/view/([^\"]+)" target="_blank">([^<]+)</a>\s*<!-- -->\s*-\s*<!-- -->\s*([^<]+)',
            section_html,
        ):
            items.append(
                {
                    "group": group,
                    "dbid": dbid,
                    "code": code.strip(),
                    "title": html.unescape(title).strip(),
                }
            )
    return items


class _Element:
    """Minimal HTML element replacing bs4.Tag for prereq-tree traversal."""
    __slots__ = ("name", "_children", "_text")

    def __init__(self, name: str) -> None:
        self.name = name
        self._children: list[_Element | str] = []
        self._text = ""

    @property
    def children(self) -> list["_Element | str"]:
        return self._children

    def get_text(self, sep: str = "", strip: bool = False) -> str:
        parts: list[str] = []
        for child in self._children:
            if isinstance(child, _Element):
                parts.append(child.get_text(sep, strip))
            else:
                parts.append(child.strip() if strip else child)
        return sep.join(p for p in parts if p)


class _TreeBuilder(_HTMLParser):
    """SAX-style builder that produces an _Element tree from an HTML string."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._root = _Element("[document]")
        self._stack: list[_Element] = [self._root]

    def handle_starttag(self, tag: str, attrs: list) -> None:
        el = _Element(tag)
        self._stack[-1]._children.append(el)
        if tag not in self.VOID:
            self._stack.append(el)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1 and self._stack[-1].name == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1]._children.append(data)


def _parse_html(markup: str) -> _Element:
    builder = _TreeBuilder()
    builder.feed(markup)
    return builder._root


def child_tags(tag: _Element, name: str | None = None) -> list[_Element]:
    children: list[_Element] = []
    for child in tag.children:
        if isinstance(child, _Element) and (name is None or child.name == name):
            children.append(child)
    return children


def leaf_rank(tag: _Element, required_rank_by_code: dict[str, int], allowed_markers: list[str]) -> int:
    text = normalize_text(tag.get_text(" ", strip=True))
    constraints: list[int] = []

    level_match = re.search(r"level\s+([1-4][AB])\s+or\s+higher", text, re.I)
    if level_match:
        constraints.append(TERM_ORDER[level_match.group(1).upper()])

    elif re.search(r"level\s+([1-4][AB])", text, re.I):
        level_match = re.search(r"level\s+([1-4][AB])", text, re.I)
        assert level_match is not None
        constraints.append(TERM_ORDER[level_match.group(1).upper()])

    if "Enrolled in" in text or "enrolled in" in text:
        if any(marker in text for marker in allowed_markers):
            constraints.append(0)
        else:
            return 99

    codes = re.findall(r"\b[A-Z]{2,5}\d{3}[A-Z]?\b", text)
    if codes:
        ranks = [term_rank_from_code(code, required_rank_by_code) for code in codes]
        if any(
            marker in text
            for marker in [
                "Must have completed at least 1 of the following",
                "Earned a minimum grade of",
                "one of the following",
                "1 of the following",
                "Choose one of the following",
                "Choose any of the following",
            ]
        ):
            known_ranks = [rank for rank in ranks if rank != 99]
            constraints.append(min(known_ranks) if known_ranks else 99)
        else:
            if any(rank == 99 for rank in ranks):
                return 99
            constraints.append(max(ranks) if ranks else 0)

    if not constraints:
        return 0
    if any(rank == 99 for rank in constraints):
        return 99
    return max(constraints)


def parse_rule(tag: _Element, required_rank_by_code: dict[str, int], allowed_markers: list[str]) -> int:
    if not isinstance(tag, _Element):
        return 0

    ul_children = child_tags(tag, "ul")
    if ul_children:
        ul = ul_children[0]
        items = [child for child in ul.children if isinstance(child, _Element)]
        child_ranks = [parse_rule(child, required_rank_by_code, allowed_markers) for child in items]
        text = normalize_text(tag.get_text(" ", strip=True))

        quantifier = re.search(r"Complete\s+(\d+)\s+of\s+the\s+following", text, re.I)
        if quantifier:
            count = int(quantifier.group(1))
            sorted_ranks = sorted(child_ranks)
            return sorted_ranks[count - 1] if len(sorted_ranks) >= count else 99

        if any(
            marker in text
            for marker in [
                "Must have completed at least 1 of the following",
                "Earned a minimum grade of",
                "one of the following",
                "1 of the following",
                "Choose one of the following",
                "Choose any of the following",
                "Complete one course from this list",
                "Complete one course from this list or an additional course from List 1",
                "Complete one course from this list or any additional course from List 1, 2, 3, or 4",
            ]
        ):
            return min(child_ranks) if child_ranks else 0

        if any(
            marker in text
            for marker in [
                "Complete all of the following",
                "Must have completed the following",
            ]
        ):
            return max(child_ranks) if child_ranks else 0

        return max(child_ranks) if child_ranks else 0

    li_children = child_tags(tag, "li")
    if li_children:
        child_ranks = [parse_rule(child, required_rank_by_code, allowed_markers) for child in li_children]
        return child_ranks[0] if len(child_ranks) == 1 else max(child_ranks)

    return leaf_rank(tag, required_rank_by_code, allowed_markers)


def classify_rank(rank: int) -> str:
    if rank == 5:
        return "3A"
    if rank == 6:
        return "3B"
    if rank >= 7 or rank == 99:
        return "impossible"
    return "2B"


def _term_code_from_alias(alias: str) -> str:
    """Convert W26/S26/F26 to UW term code ('1261'). Also accepts raw 4-digit codes."""
    alias = alias.strip().upper()
    if re.fullmatch(r"\d{4}", alias):
        return alias
    m = re.fullmatch(r"([WSF])(\d{2})", alias)
    if not m:
        raise ValueError(f"Unrecognized term format: {alias!r}. Use a format like W26, S26, or F25.")
    season_char, yy = m.group(1), int(m.group(2))
    year = 2000 + yy
    suffix = {"W": 1, "S": 5, "F": 9}[season_char]
    return str(1000 + (year - 2000) * 10 + suffix)


def _term_code_to_display(code: str) -> str:
    """Convert UW term code back to friendly name like 'winter2026'."""
    if not re.fullmatch(r"\d{4}", code):
        return code
    code_int = int(code)
    suffix = code_int % 10
    year = 2000 + (code_int - 1000 - suffix) // 10
    season = {1: "winter", 5: "spring", 9: "fall"}.get(suffix)
    return f"{season}{year}" if season else code


def term_display_name(term: str) -> str:
    return _term_code_to_display(term)


def _print_progress(label: str, current: int, total: int, width: int = 28) -> None:
    filled = int(width * current / total) if total else width
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    print(f"\r{label}  [{bar}] {current}/{total}", end="", flush=True, file=sys.stderr)
    if current >= total:
        print(file=sys.stderr)


def prompt_non_empty(prompt_text: str, *, validator: Callable[[str], str] | None = None) -> str:
    while True:
        value = input(prompt_text).strip()
        if not value:
            print("Input cannot be empty. Please try again.")
            continue
        if validator is not None:
            try:
                normalized = validator(value)
            except ValueError as error:
                print(str(error))
                continue
            if normalized is not None:
                return normalized
            return value
        return value


def normalize_standing(value: str) -> str:
    cleaned = value.strip().upper().replace(" ", "")
    if not re.fullmatch(r"[1-4][AB]", cleaned):
        raise ValueError("Invalid standing. Please enter something like 2B, 3A, 3B, or 4A.")
    return cleaned


def prompt_yes_no(prompt_text: str) -> bool:
    while True:
        value = input(prompt_text).strip().lower()
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please enter yes or no (y/n).")


def split_registered_course_entries(raw_text: str) -> list[str]:
    entries = [part.strip() for part in re.split(r"[\n,;]+", raw_text) if part.strip()]
    return entries


def parse_registered_course_entry(raw_entry: str) -> RegisteredCourseEntry:
    cleaned = normalize_text(raw_entry).upper()
    match = re.search(r"\b([A-Z]{2,5}\d{2,4}[A-Z]?)\b", cleaned)
    if not match:
        raise ValueError(f"Could not parse course code: {raw_entry!r}. Use a format like MATH239 or ECE250 LEC 001.")

    code = match.group(1)
    remainder = cleaned[match.end():].strip()
    remainder = remainder.lstrip(":-/").strip()
    component: str | None = None
    class_num: str | None = None

    if remainder:
        parts = re.findall(r"[A-Z]+|\d+", remainder)
        if len(parts) == 1:
            if parts[0].isdigit():
                class_num = parts[0]
            else:
                component = parts[0]
        else:
            component = parts[0]
            if parts[1].isdigit():
                class_num = parts[1]

    return RegisteredCourseEntry(raw=raw_entry.strip(), code=code, component=component, class_num=class_num)


def prompt_registered_courses() -> tuple[RegisteredCourseEntry, ...]:
    print("Enter your currently registered courses, one per line.")
    print("Accepted formats:")
    print("  COURSE            e.g. MATH239  (all sections treated as occupied)")
    print("  COURSE COMP SEC   e.g. ECE250 LEC 001  (only that section)")
    print("You may also separate entries with commas or semicolons.")
    print("Press Enter on a blank line when done.")

    lines: list[str] = []
    while True:
        line = input("course> ").strip()
        if not line:
            break
        lines.append(line)

    raw_entries = split_registered_course_entries("\n".join(lines))
    entries = [parse_registered_course_entry(entry) for entry in raw_entries]
    if not entries:
        raise ValueError("No valid course entries were provided.")
    return tuple(entries)


def serialize_section_row(row: ScheduleSectionRow) -> dict[str, str | bool]:
    return {
        "class_num": row.class_num,
        "component": row.component,
        "camp_loc": row.camp_loc,
        "assoc_class": row.assoc_class,
        "rel1": row.rel1,
        "rel2": row.rel2,
        "enrl_cap": row.enrl_cap,
        "enrl_tot": row.enrl_tot,
        "wait_cap": row.wait_cap,
        "wait_tot": row.wait_tot,
        "time_days_date": row.time_days_date,
        "bldg_room": row.bldg_room,
        "instructor": row.instructor,
        "is_online": row.is_online,
    }


def deserialize_section_row(section_row: dict[str, str | bool]) -> ScheduleSectionRow:
    return ScheduleSectionRow(
        class_num=str(section_row.get("class_num", "")),
        component=str(section_row.get("component", "")),
        camp_loc=str(section_row.get("camp_loc", "")),
        assoc_class=str(section_row.get("assoc_class", "")),
        rel1=str(section_row.get("rel1", "")),
        rel2=str(section_row.get("rel2", "")),
        enrl_cap=str(section_row.get("enrl_cap", "")),
        enrl_tot=str(section_row.get("enrl_tot", "")),
        wait_cap=str(section_row.get("wait_cap", "")),
        wait_tot=str(section_row.get("wait_tot", "")),
        time_days_date=str(section_row.get("time_days_date", "")),
        bldg_room=str(section_row.get("bldg_room", "")),
        instructor=str(section_row.get("instructor", "")),
    )


def parse_time_days_date(value: str) -> tuple[int, int, tuple[str, ...]] | None:
    text = normalize_text(value)
    if not text or text == "TBA":
        return None

    match = re.match(r"(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})(?P<days>.*)", text)
    if not match:
        return None

    days_text = match.group("days") or ""
    if re.search(r"\d|/", days_text):
        return None
    days = tuple(re.findall(r"Th|[MTWFS]", days_text))
    if not days:
        return None

    start_hour, start_minute = [int(part) for part in match.group("start").split(":")]
    end_hour, end_minute = [int(part) for part in match.group("end").split(":")]
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute, days


def has_meeting_info(value: str) -> bool:
    text = normalize_text(value)
    return bool(text and text != "TBA")


def split_component_and_section(value: str) -> tuple[str, str | None]:
    cleaned = normalize_text(value).upper()
    match = re.fullmatch(r"([A-Z]+)(?:\s+(\d{3}[A-Z]?))?", cleaned)
    if not match:
        return cleaned, None
    return match.group(1), match.group(2)


def row_to_blocks(row: ScheduleSectionRow, *, label: str) -> tuple[MeetingBlock, ...]:
    if row.is_online:
        return ()

    parsed = parse_time_days_date(row.time_days_date)
    if parsed is None:
        return ()

    start_minute, end_minute, days = parsed
    return (MeetingBlock(days=days, start_minute=start_minute, end_minute=end_minute, label=label),)


def blocks_overlap(left: MeetingBlock, right: MeetingBlock) -> bool:
    if not set(left.days).intersection(right.days):
        return False
    return left.start_minute < right.end_minute and right.start_minute < left.end_minute


def row_conflict_labels(row: ScheduleSectionRow, occupied_blocks: tuple[MeetingBlock, ...]) -> tuple[str, ...]:
    if row.is_online:
        return ()

    parsed = parse_time_days_date(row.time_days_date)
    if parsed is None:
        return ("unknown-time",)

    start_minute, end_minute, days = parsed
    candidate = MeetingBlock(days=days, start_minute=start_minute, end_minute=end_minute, label="")
    matches = [block.label for block in occupied_blocks if blocks_overlap(candidate, block)]
    return tuple(dict.fromkeys(matches))


def is_row_selected(row: ScheduleSectionRow, entry: RegisteredCourseEntry) -> bool:
    row_component, row_section = split_component_and_section(row.component)

    if entry.component is None and entry.class_num is None:
        return True
    if entry.component is not None and row_component != entry.component:
        return False
    if entry.class_num is not None and row.class_num != entry.class_num and row_section != entry.class_num:
        return False
    return True


def build_occupied_blocks(entries: tuple[RegisteredCourseEntry, ...], term: str) -> tuple[MeetingBlock, ...]:
    occupied: list[MeetingBlock] = []
    for entry in entries:
        try:
            subject, cournum = schedule_parse_course(entry.code)
            html_text = schedule_fetch_schedule("under", term, subject, cournum)
            if schedule_extract_no_matches(html_text):
                continue
            _, rows = schedule_extract_section_table(html_text)
        except Exception:
            continue

        selected_rows = [row for row in rows if is_row_selected(row, entry)]
        if not selected_rows:
            selected_rows = rows

        for row in selected_rows:
            occupied.extend(row_to_blocks(row, label=entry.code if entry.component is None else f"{entry.code} {entry.component} {entry.class_num or ''}".strip()))

    return tuple(occupied)


def conflict_status_for_record(record: ScheduleRecord, occupied_blocks: tuple[MeetingBlock, ...]) -> tuple[str, str]:
    rows = [deserialize_section_row(section_row) for section_row in record.section_rows]
    if not rows:
        return "with conflict", "no timetable data could be parsed"

    grouped: dict[str, list[ScheduleSectionRow]] = defaultdict(list)
    for row in rows:
        grouped[row.component].append(row)

    for component in sorted(grouped):
        component_rows = grouped[component]
        component_fits = False
        first_conflict: tuple[str, ...] = ()

        for row in component_rows:
            conflicts = row_conflict_labels(row, occupied_blocks)
            if not conflicts:
                component_fits = True
                break
            if not first_conflict:
                first_conflict = conflicts

        if not component_fits:
            if first_conflict == ("unknown-time",):
                return "with conflict", f"{component} has only TBA/unknown meeting times"
            return "with conflict", f"{component} conflicts with registered course(s): {', '.join(first_conflict)}"

    return "no conflict", "at least one non-conflicting option exists for every required component"


def _fetch_all_programs(catalog_id: str) -> list[dict]:
    url = f"https://uwaterloocm.kuali.co/api/v1/catalog/programs/{catalog_id}"
    try:
        return fetch_json(url)  # type: ignore[return-value]
    except Exception:
        return []


def _score_program(program: dict, query: str) -> int:
    title = program.get("title", "").lower()
    query_lower = query.lower()
    words = query_lower.split()
    score = 0
    for word in words:
        if len(word) <= 3:
            # short tokens must appear as whole word (bounded by non-alpha)
            if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", title):
                score += 10
        elif word in title:
            score += 10
    if query_lower in title:
        score += 20
    if title.startswith(query_lower):
        score += 10
    return score


def prompt_program_info() -> tuple[str, str, str]:
    """Interactive fuzzy program search. Returns (catalog_id, program_pid, program_name)."""
    print("Fetching program list…")
    try:
        _debug_print(f"Resolving catalog id from {CATALOG_PAGE_URL}")
        catalog_id = resolve_catalog_id()
        _debug_print(f"Resolved catalog id: {catalog_id}")
        _debug_print(f"Fetching programs from {BASE_URL}/programs/{catalog_id}")
        programs = _fetch_all_programs(catalog_id)
        _debug_print(f"Fetched {len(programs)} program records")
    except Exception as error:
        _debug_exception("Failed to fetch program list", error)
        programs = []
        catalog_id = ""
    if not programs:
        print("Could not fetch program list. Please provide identifiers manually.")
        catalog_id = prompt_non_empty("Catalog ID: ")
        program_pid = prompt_non_empty("Program PID: ")
        program_name = prompt_non_empty("Program name: ")
        return catalog_id, program_pid, program_name

    top: list[dict] = []
    while True:
        answer = input("Your program (type a keyword to search): ").strip()
        if not answer:
            print("Please enter a keyword.")
            continue

        scored = sorted(
            [p for p in programs if _score_program(p, answer) > 0],
            key=lambda p: _score_program(p, answer),
            reverse=True,
        )

        if not scored:
            print(f"No programs matched {answer!r}. Try a different keyword.")
            continue

        if len(scored) == 1:
            chosen = scored[0]
            print(f"Selected: {chosen['title']}")
            return catalog_id, chosen["pid"], chosen["title"]

        top = scored[:10]
        print(f"\n{len(scored)} match(es) found, showing top {len(top)}:")
        for i, p in enumerate(top, 1):
            print(f"  {i}. {p['title']}")
        print()
        break

    while True:
        sel = input("Enter a number to select, or type a new keyword to search: ").strip()
        if sel.isdigit():
            idx = int(sel)
            if 1 <= idx <= len(top):
                chosen = top[idx - 1]
                return catalog_id, chosen["pid"], chosen["title"]
            print(f"Please enter a number between 1 and {len(top)}.")
        elif sel:
            scored2 = sorted(
                [p for p in programs if _score_program(p, sel) > 0],
                key=lambda p: _score_program(p, sel),
                reverse=True,
            )
            if not scored2:
                print(f"No programs matched {sel!r}. Try again.")
                continue
            if len(scored2) == 1:
                chosen = scored2[0]
                print(f"Selected: {chosen['title']}")
                return catalog_id, chosen["pid"], chosen["title"]
            top = scored2[:10]
            print(f"\n{len(scored2)} match(es) found, showing top {len(top)}:")
            for i, p in enumerate(top, 1):
                print(f"  {i}. {p['title']}")
            print()


def prompt_completed_electives() -> tuple[RegisteredCourseEntry, ...]:
    print("Enter any electives you completed in previous terms, one per line.")
    print("Same format as registered courses (COURSE or COURSE COMP SEC).")
    print("Press Enter on a blank line when done.")
    lines: list[str] = []
    while True:
        line = input("completed> ").strip()
        if not line:
            break
        lines.append(line)
    raw_entries = split_registered_course_entries("\n".join(lines))
    if not raw_entries:
        return ()
    return tuple(parse_registered_course_entry(entry) for entry in raw_entries)


def summarize_schedule_rows(rows: list[ScheduleSectionRow]) -> tuple[str, tuple[str, ...]]:
    if not rows:
        return "n/a", ("No matched sections were found.",)

    any_online = any(row.is_online for row in rows)
    any_in_person = any(not row.is_online for row in rows)
    if any_online and not any_in_person:
        status = "online"
    else:
        status = "in-person" if any_in_person else "mixed"

    lines: list[str] = []
    for row in rows:
        meeting = row.time_days_date or "TBA"
        location = row.bldg_room or row.camp_loc or "TBA"
        if row.is_online:
            location = "ONLINE"
        lines.append(f"{row.component} {row.class_num}: {meeting} | {location} | {row.instructor or 'TBA'}")

    return status, tuple(lines)


def campus_group_from_rows(rows: list[ScheduleSectionRow]) -> str:
    if not rows:
        return "n/a"

    if all(row.is_online for row in rows):
        return "online"

    non_online_rows = [row for row in rows if not row.is_online]
    if all(row.camp_loc == "UW U" for row in non_online_rows):
        return "UW U"

    return "other"


def lookup_schedule_record(record: CourseRecord, term: str) -> ScheduleRecord:
    try:
        subject, cournum = schedule_parse_course(record.code)
        html_text = schedule_fetch_schedule("under", term, subject, cournum)
        if schedule_extract_no_matches(html_text):
            return ScheduleRecord(
                group=record.group,
                code=record.code,
                title=record.title,
                category=record.category,
                campus_group="n/a",
                status="n/a",
                term=term,
                section_lines=("No matched sections were found.",),
                section_rows=(),
            )

        _, rows = schedule_extract_section_table(html_text)
        status, lines = summarize_schedule_rows(rows)
        campus_group = campus_group_from_rows(rows)
        return ScheduleRecord(
            group=record.group,
            code=record.code,
            title=record.title,
            category=record.category,
            campus_group=campus_group,
            status=status,
            term=term,
            section_lines=lines,
            section_rows=tuple(serialize_section_row(row) for row in rows),
        )
    except Exception as error:  # pragma: no cover - network/HTML fallback path
        return ScheduleRecord(
            group=record.group,
            code=record.code,
            title=record.title,
            category=record.category,
            campus_group="n/a",
            status="n/a",
            term=term,
            section_lines=(f"Lookup failed: {error}",),
            section_rows=(),
        )


def make_source_url(catalog_id: str, program_pid: str, program_name: str) -> str:
    current = quote(program_name, safe="")
    return (
        "https://uwaterloo.ca/academic-calendar/undergraduate-studies/catalog#/programs/"
        f"{program_pid}?bc=true&bcCurrent={current}&bcGroup={current}&bcItemType=programs"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="elective-scout: find Waterloo electives you can actually take this term.",
        epilog="Run without any flags to use interactive mode. All flags below are for scripting or debugging.",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Disable all prompts; use only the flags below")
    # Program (override interactive prompts)
    parser.add_argument("--catalog-id", default=None, help="Kuali catalog id")
    parser.add_argument("--program-pid", default=None, help="Kuali program pid")
    parser.add_argument("--program-name", default=None, help="Program display name")
    # Output
    parser.add_argument("--output-dir", default=".", help="Directory for generated files (default: current directory)")
    parser.add_argument("--output-prefix", default=None, help="Prefix for output filenames (default: derived from program name)")
    parser.add_argument("--report", action="store_true", help="Write classification and schedule files to disk")
    parser.add_argument("--verbose", action="store_true", help="Show course names in terminal output (default: codes only)")
    parser.add_argument("--uwflow", action="store_true", help="Show UW Flow easy/useful scores next to each terminal-listed course")
    # Schedule
    parser.add_argument("--schedule-term", help="Waterloo schedule term number or named alias")
    parser.add_argument("--allowed-marker", action="append", dest="allowed_markers", help="Additional prerequisite enrollment marker to treat as satisfied")
    # Student (override interactive prompts; --student-conflict-check kept for back-compat)
    parser.add_argument("--student-conflict-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--student-major-name", default=None, help="Student major full name")
    parser.add_argument("--student-standing", default=None, help="Student standing such as 2B or 3A")
    parser.add_argument("--student-registered-course", action="append", dest="student_registered_courses", help="Current-semester registered course; repeat for multiple")
    parser.add_argument("--student-completed-course", action="append", dest="student_completed_courses", help="Past-semester completed elective; repeat for multiple")
    return parser


def _slug_from_name(name: str) -> str:
    words = re.findall(r"[A-Za-z]+", name)
    if not words:
        return "output"
    return "_".join(w.lower() for w in words[:3])


def prompt_schedule_term() -> str:
    """Ask the user for a term alias and return the resolved UW term code."""
    while True:
        raw = input("Schedule term (e.g. W26, S26, F26; Enter for current term): ").strip()
        if not raw:
            return schedule_default_term()
        try:
            return _term_code_from_alias(raw)
        except ValueError as error:
            print(str(error))


def _describe_section_option(row: ScheduleSectionRow) -> str:
    _, section = split_component_and_section(row.component)
    meeting = row.time_days_date or "TBA"
    location = row.bldg_room or row.camp_loc or "TBA"
    if row.is_online:
        location = "ONLINE"
    section_text = section or "?"
    return f"section {section_text} | class {row.class_num} | {meeting} | {location} | {row.instructor or 'TBA'}"


def _section_family_key(row: ScheduleSectionRow) -> str:
    component, section = split_component_and_section(row.component)
    if section and re.fullmatch(r"[0-3]\d\d", section):
        return section[0]
    return component


def _section_family_label(family_key: str, options: list[ScheduleSectionRow]) -> str:
    if family_key == "0":
        return "LEC"
    if family_key == "1":
        return "TUT"
    if family_key == "2":
        return "LAB"
    if family_key == "3":
        components = sorted({row.component for row in options})
        return "/".join(components) if components else "SEC"
    components = sorted({split_component_and_section(row.component)[0] for row in options})
    return "/".join(components) if components else family_key


def prompt_required_course_sections(course_codes: tuple[str, ...], term: str) -> tuple[RegisteredCourseEntry, ...]:
    entries: list[RegisteredCourseEntry] = []
    if not course_codes:
        return ()

    print("\nSelect your sections for this term's required courses.")
    print("Components with no meeting information are skipped.")
    print("One-off dated components may still be asked so you can identify your actual section.")

    for code in course_codes:
        subject, cournum = schedule_parse_course(code)
        html_text = schedule_fetch_schedule("under", term, subject, cournum)
        if schedule_extract_no_matches(html_text):
            print(f"\n{code}: no schedule rows were found for {term_display_name(term)}; skipping section prompt.")
            continue

        _, rows = schedule_extract_section_table(html_text)
        grouped: dict[str, list[ScheduleSectionRow]] = defaultdict(list)
        for row in rows:
            if not has_meeting_info(row.time_days_date):
                continue
            grouped[_section_family_key(row)].append(row)

        if not grouped:
            continue

        print(f"\n{code}")
        for family_key in sorted(grouped):
            options = sorted(grouped[family_key], key=lambda row: (row.class_num, row.time_days_date, row.bldg_room))
            label = _section_family_label(family_key, options)

            if len(options) == 1:
                chosen = options[0]
                chosen_component, chosen_section = split_component_and_section(chosen.component)
                entries.append(
                    RegisteredCourseEntry(
                        raw=f"{code} {chosen_component} {chosen_section or chosen.class_num}",
                        code=code,
                        component=chosen_component,
                        class_num=chosen_section or chosen.class_num,
                    )
                )
                print(f"  {code} {label}: using section {chosen_section or chosen.class_num} (only regular weekly option).")
                continue

            print(f"  {label} options:")
            for row in options:
                print(f"    {row.class_num}: {_describe_section_option(row)}")

            family_hint = f"({family_key}xx)" if family_key in {"0", "1", "2", "3"} else ""
            while True:
                answer = input(f"  Enter {code} {label} section {family_hint}: ").strip()
                matches = []
                for row in options:
                    _, section = split_component_and_section(row.component)
                    if section == answer:
                        matches.append(row)
                if matches:
                    chosen = matches[0]
                    chosen_component, chosen_section = split_component_and_section(chosen.component)
                    entries.append(
                        RegisteredCourseEntry(
                            raw=f"{code} {chosen_component} {chosen_section or chosen.class_num}",
                            code=code,
                            component=chosen_component,
                            class_num=chosen_section or chosen.class_num,
                        )
                    )
                    break
                print("Please enter one of the listed section numbers.")

    return tuple(entries)


def main() -> None:
    args = build_parser().parse_args()
    interactive = not args.non_interactive

    # --- STEP 1: Resolve program identity ---
    if args.catalog_id and args.program_pid and args.program_name:
        catalog_id = args.catalog_id
        program_pid = args.program_pid
        program_name = args.program_name
    elif interactive:
        catalog_id, program_pid, program_name = prompt_program_info()
    else:
        missing = [
            name
            for name, value in (
                ("--catalog-id", args.catalog_id),
                ("--program-pid", args.program_pid),
                ("--program-name", args.program_name),
            )
            if not value
        ]
        if missing:
            raise SystemExit("Non-interactive mode requires " + ", ".join(missing) + ".")
        catalog_id = args.catalog_id
        program_pid = args.program_pid
        program_name = args.program_name

    allowed_markers = build_allowed_markers(program_name)
    if args.allowed_markers:
        allowed_markers.extend(marker for marker in args.allowed_markers if marker)

    output_prefix = args.output_prefix or _slug_from_name(program_name)

    # --- STEP 2: Collect student profile BEFORE classification ---
    # The student's major resolves "Enrolled in X" enrollment prerequisites.
    # Their completed electives resolve specific course-code prerequisites.
    # Both must be known before the impossible/accessible judgment can be accurate.
    student_profile: StudentProfile | None = None
    scripted_student = bool(
        args.student_conflict_check
        or args.student_major_name
        or args.student_standing
        or args.student_registered_courses
        or args.student_completed_courses
    )

    if interactive or scripted_student:
        student_major_name = (args.student_major_name or program_name).strip()

        if args.student_standing:
            student_standing = normalize_standing(args.student_standing)
        else:
            student_standing = prompt_non_empty(
                "Your current/upcoming term (e.g. 2B, 3A, 3B, 4A): ",
                validator=normalize_standing,
            )

        # Completed electives (past semesters) → used to resolve prereq conditions
        if args.student_completed_courses is not None and args.student_completed_courses:
            completed_elective_entries = tuple(
                parse_registered_course_entry(entry)
                for entry in args.student_completed_courses
            )
        elif not interactive:
            completed_elective_entries = ()
        else:
            has_completed = prompt_yes_no(
                "Have you taken any non-required electives in previous terms? (y/n): "
            )
            completed_elective_entries = prompt_completed_electives() if has_completed else ()

        # Registered courses (current semester) → used for timetable conflict checking
        if args.student_registered_courses is not None and args.student_registered_courses:
            registered_entries = tuple(
                parse_registered_course_entry(entry)
                for entry in args.student_registered_courses
            )
        elif not interactive:
            registered_entries = ()
        else:
            has_registered = prompt_yes_no(
                "Have you already registered for any additional courses this term? (y/n): "
            )
            registered_entries = prompt_registered_courses() if has_registered else ()

        student_profile = StudentProfile(
            major_name=student_major_name,
            standing=student_standing,
            completed_elective_entries=completed_elective_entries,
            registered_entries=registered_entries,
        )

        # Augment allowed_markers with the student's own major so that
        # "Enrolled in X" prereqs resolve correctly for non-EE programs.
        if student_profile.major_name not in allowed_markers:
            allowed_markers.append(student_profile.major_name)

        print(f"\nProgram: {student_profile.major_name}")
        print(f"Standing: {student_profile.standing}")
        if student_profile.completed_elective_entries:
            print("Completed electives: " + ", ".join(e.code for e in student_profile.completed_elective_entries))
        if student_profile.registered_entries:
            print("Also registered: " + ", ".join(
                f"{e.code}{' ' + e.component if e.component else ''}{' ' + e.class_num if e.class_num else ''}".strip()
                for e in student_profile.registered_entries
            ))

    # --- STEP 2b: Resolve schedule term ---
    if args.schedule_term:
        schedule_term = _term_code_from_alias(args.schedule_term)
    elif interactive:
        schedule_term = prompt_schedule_term()
    else:
        schedule_term = schedule_default_term()

    # --- STEP 3: Fetch program data and build classification context ---
    program = fetch_json(f"{BASE_URL}/program/{catalog_id}/{program_pid}")
    required_rank_by_code = build_required_rank_map(program)

    # All required courses from semesters strictly before the student's current standing
    # have already been completed. Mark them rank=0 so they never block a prereq check.
    if student_profile:
        student_standing_rank = TERM_ORDER.get(student_profile.standing, 0)
        for code in list(required_rank_by_code):
            if required_rank_by_code[code] < student_standing_rank:
                required_rank_by_code[code] = 0

    # Additionally, any manually-listed completed electives (non-required courses from
    # past semesters) are also treated as satisfied prereqs.
    if student_profile and student_profile.completed_elective_entries:
        for entry in student_profile.completed_elective_entries:
            required_rank_by_code[entry.code] = 0

    raw_items = extract_course_list_items(program)

    unique_items: list[dict[str, str]] = []
    seen_dbids: set[str] = set()
    for item in raw_items:
        if item["dbid"] in seen_dbids:
            continue
        seen_dbids.add(item["dbid"])
        unique_items.append(item)

    records: list[CourseRecord] = []
    course_payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        future_map = {
            executor.submit(fetch_json, f"{BASE_URL}/course/byId/{catalog_id}/{item['dbid']}"): item["dbid"]
            for item in unique_items
        }
        for index, future in enumerate(as_completed(future_map), start=1):
            dbid = future_map[future]
            course_payloads[dbid] = future.result()
            _print_progress("Fetching courses ", index, len(unique_items))

    for item in unique_items:
        dbid = item["dbid"]
        course = course_payloads[dbid]
        soup = _parse_html(course.get("prerequisites", ""))
        rank = parse_rule(soup, required_rank_by_code, allowed_markers)
        category = classify_rank(rank)
        records.append(
            CourseRecord(
                group=item["group"],
                dbid=dbid,
                code=item["code"],
                title=course.get("title", item["title"]),
                prereq_text=normalize_text(soup.get_text(" ", strip=True)),
                rank=rank,
                category=category,
            )
        )

    out_dir = Path(args.output_dir).resolve()
    if args.report:
        out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{output_prefix}_classification.json"
    md_path = out_dir / f"{output_prefix}_classification.md"
    schedule_json_path = out_dir / f"{output_prefix}_schedule_classification.json"
    schedule_md_path = out_dir / f"{output_prefix}_schedule_classification.md"

    if args.report:
        json_path.write_text(json.dumps([record.__dict__ for record in records], ensure_ascii=False, indent=2), encoding="utf-8")

    grouped: dict[str, list[CourseRecord]] = defaultdict(list)
    for record in records:
        grouped[record.category].append(record)

    lines: list[str] = []
    lines.append(f"# {program_name} Electives Classification")
    lines.append("")
    lines.append(f"Source: [{program_name}]({make_source_url(catalog_id, program_pid, program_name)})")
    lines.append("")
    if student_profile:
        lines.append(f"Student: {student_profile.major_name}, standing {student_profile.standing}")
        if student_profile.completed_elective_entries:
            lines.append("Completed electives: " + ", ".join(e.code for e in student_profile.completed_elective_entries))
        lines.append("")
        lines.append("Classification rule: earliest term where the prerequisite tree is satisfied, taking into account this student's major and completed elective courses.")
    else:
        lines.append("Classification rule: earliest term where the official prerequisite tree is satisfied by the program required-course path, ignoring approvals, timetable conflicts, and seat availability.")
    lines.append("")

    counts = Counter(record.category for record in records)
    lines.append("## Summary")
    for category in ["2B", "3A", "3B", "impossible"]:
        lines.append(f"- {category}: {counts.get(category, 0)}")
    lines.append("")

    for category in ["2B", "3A", "3B", "impossible"]:
        items = sorted(grouped.get(category, []), key=lambda record: (record.group, record.code))
        lines.append(f"## {category}")
        if not items:
            lines.append("- None")
            lines.append("")
            continue
        current_group = None
        for record in items:
            if record.group != current_group:
                current_group = record.group
                lines.append(f"### {current_group}")
            lines.append(f"- {record.code} - {record.title} | {record.prereq_text or 'No listed prerequisites'}")
        lines.append("")

    if args.report:
        md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        print(f"Wrote {md_path}")
        print(f"Wrote {json_path}")

    # --- STEP 3: Schedule lookup for non-impossible courses ---
    non_impossible_records = [record for record in records if record.category != "impossible"]
    schedule_records: list[ScheduleRecord] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(lookup_schedule_record, record, schedule_term): record
            for record in non_impossible_records
        }
        for index, future in enumerate(as_completed(future_map), start=1):
            schedule_records.append(future.result())
            _print_progress("Fetching schedules", index, len(non_impossible_records))

    schedule_records.sort(key=lambda record: (record.campus_group, record.category, record.group, record.code))
    if args.report:
        schedule_json_path.write_text(
            json.dumps([record.__dict__ for record in schedule_records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    schedule_grouped: dict[str, dict[str, list[ScheduleRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in schedule_records:
        schedule_grouped[record.campus_group][record.category].append(record)

    schedule_lines: list[str] = []
    schedule_lines.append(f"# {program_name} Schedule Classification")
    schedule_lines.append("")
    schedule_lines.append(f"Source: [{program_name}]({make_source_url(catalog_id, program_pid, program_name)})")
    schedule_lines.append("")
    schedule_lines.append(f"Scope: non-impossible courses from the electives classification, checked against term {schedule_term} ({term_display_name(schedule_term)}).")
    schedule_lines.append("")

    schedule_counts = Counter(record.campus_group for record in schedule_records)
    schedule_lines.append("## Summary")
    for campus_group in ["online", "other", "UW U", "n/a"]:
        schedule_lines.append(f"- {campus_group}: {schedule_counts.get(campus_group, 0)}")
    schedule_lines.append("")

    for campus_group in ["online", "other", "UW U", "n/a"]:
        schedule_lines.append(f"## {campus_group}")
        grouped_by_category = schedule_grouped.get(campus_group, {})
        if not grouped_by_category:
            schedule_lines.append("- None")
            schedule_lines.append("")
            continue

        for category in ["2B", "3A", "3B"]:
            items = sorted(grouped_by_category.get(category, []), key=lambda record: (record.group, record.code))
            if not items:
                continue
            schedule_lines.append(f"### {category}")
            for record in items:
                detail = "; ".join(record.section_lines)
                schedule_lines.append(f"- {record.code} - {record.title} | {record.group} | {detail}")
        schedule_lines.append("")

    if args.report:
        schedule_md_path.write_text("\n".join(schedule_lines).rstrip() + "\n", encoding="utf-8")
        print(f"Wrote {schedule_md_path}")
        print(f"Wrote {schedule_json_path}")

    # --- STEP 4: Timetable conflict check (student profile already collected in STEP 1) ---
    conflict_records: list[dict[str, object]] = []
    if student_profile is not None:
        conflict_json_path = out_dir / f"{output_prefix}_uwu_conflict_classification.json"
        conflict_md_path = out_dir / f"{output_prefix}_uwu_conflict_classification.md"

        uwu_records = [record for record in schedule_records if record.campus_group == "UW U"]

        student_standing_rank = TERM_ORDER.get(student_profile.standing, 0)
        auto_required_codes = tuple(
            code
            for code, rank in required_rank_by_code.items()
            if rank == student_standing_rank
        )

        if interactive:
            auto_required_entries = prompt_required_course_sections(auto_required_codes, schedule_term)
        else:
            required_code_set = set(auto_required_codes)
            provided_code_set = {entry.code for entry in student_profile.registered_entries}
            missing_required = [code for code in auto_required_codes if code not in provided_code_set]
            if missing_required:
                raise SystemExit(
                    "Non-interactive conflict check requires explicit --student-registered-course entries for all current-term required courses: "
                    + ", ".join(missing_required)
                )
            auto_required_entries = tuple(
                entry for entry in student_profile.registered_entries
                if entry.code in required_code_set
            )

        extra_registered_entries = tuple(
            entry for entry in student_profile.registered_entries
            if entry.code not in set(auto_required_codes)
        )
        all_registered_entries = auto_required_entries + extra_registered_entries
        occupied_blocks = build_occupied_blocks(all_registered_entries, schedule_term) if all_registered_entries else ()
        conflict_records: list[dict[str, object]] = []

        for record in uwu_records:
            conflict_status, conflict_reason = conflict_status_for_record(record, occupied_blocks)

            conflict_records.append(
                {
                    "group": record.group,
                    "code": record.code,
                    "title": record.title,
                    "category": record.category,
                    "campus_group": record.campus_group,
                    "status": record.status,
                    "term": record.term,
                    "section_lines": list(record.section_lines),
                    "conflict_status": conflict_status,
                    "conflict_reason": conflict_reason,
                }
            )

        conflict_records.sort(key=lambda item: (item["conflict_status"], item["category"], item["group"], item["code"]))
        if args.report:
            conflict_json_path.write_text(
                json.dumps(
                    {
                        "student": {
                            "major_name": student_profile.major_name,
                            "standing": student_profile.standing,
                            "completed_elective_courses": [entry.__dict__ for entry in student_profile.completed_elective_entries],
                            "registered_courses": [entry.__dict__ for entry in extra_registered_entries],
                            "required_course_sections": [entry.__dict__ for entry in auto_required_entries],
                        },
                        "summary": {
                            "no conflict": sum(1 for item in conflict_records if item["conflict_status"] == "no conflict"),
                            "with conflict": sum(1 for item in conflict_records if item["conflict_status"] == "with conflict"),
                        },
                        "records": conflict_records,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        conflict_lines: list[str] = []
        conflict_lines.append(f"# {program_name} UW U Conflict Classification")
        conflict_lines.append("")
        conflict_lines.append(f"Source: [{program_name}]({make_source_url(catalog_id, program_pid, program_name)})")
        conflict_lines.append("")
        conflict_lines.append(f"Student major: {student_profile.major_name}")
        conflict_lines.append(f"Student standing: {student_profile.standing}")
        if student_profile.completed_elective_entries:
            conflict_lines.append("Completed electives: " + ", ".join(e.code for e in student_profile.completed_elective_entries))
        if auto_required_entries:
            conflict_lines.append("Required courses this term: " + ", ".join(
                f"{e.code} {e.component or ''} {e.class_num or ''}".strip()
                for e in auto_required_entries
            ))
        if extra_registered_entries:
            conflict_lines.append("Additional registered courses: " + ", ".join(
                f"{e.code}{' ' + e.component if e.component else ''}{' ' + e.class_num if e.class_num else ''}".strip()
                for e in extra_registered_entries
            ))
        conflict_lines.append(f"Schedule term: {schedule_term} ({term_display_name(schedule_term)})")
        conflict_lines.append("")
        conflict_lines.append("## Summary")
        conflict_lines.append(f"- no conflict: {sum(1 for item in conflict_records if item['conflict_status'] == 'no conflict')}")
        conflict_lines.append(f"- with conflict: {sum(1 for item in conflict_records if item['conflict_status'] == 'with conflict')}")
        conflict_lines.append("")

        conflict_grouped: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(lambda: defaultdict(list))
        for item in conflict_records:
            conflict_grouped[str(item["conflict_status"])][str(item["category"])].append(item)

        for conflict_status in ["no conflict", "with conflict"]:
            conflict_lines.append(f"## {conflict_status}")
            grouped_by_category = conflict_grouped.get(conflict_status, {})
            if not grouped_by_category:
                conflict_lines.append("- None")
                conflict_lines.append("")
                continue

            for category in ["2B", "3A", "3B"]:
                items = sorted(grouped_by_category.get(category, []), key=lambda item: (str(item["group"]), str(item["code"])))
                if not items:
                    continue
                conflict_lines.append(f"### {category}")
                for item in items:
                    detail = "; ".join(item["section_lines"]) if item.get("section_lines") else "No matched sections were found."
                    reason = str(item["conflict_reason"])
                    conflict_lines.append(f"- {item['code']} - {item['title']} | {item['group']} | {detail} | {reason}")
                conflict_lines.append("")

        if args.report:
            conflict_md_path.write_text("\n".join(conflict_lines).rstrip() + "\n", encoding="utf-8")
            print(f"Wrote {conflict_md_path}")
            print(f"Wrote {conflict_json_path}")

    # --- Terminal summary: eligible electives ---
    online_records = [
        r for r in schedule_records
        if any(bool(section_row.get("is_online")) for section_row in r.section_rows)
    ]
    no_conflict_items = [item for item in conflict_records if item["conflict_status"] == "no conflict"]
    uwflow_cache: dict[str, UWFlowStats] = {}

    def get_uwflow_stats(code: str) -> UWFlowStats:
        if code not in uwflow_cache:
            uwflow_cache[code] = fetch_uwflow_stats(code)
        return uwflow_cache[code]

    print(f"\nEligible electives \u2014 {term_display_name(schedule_term)}")

    print(f"\nonline ({len(online_records)}):")
    if online_records:
        if args.uwflow:
            for r in sorted(online_records, key=lambda r: r.code):
                base = f"  {r.code} - {r.title}" if args.verbose else f"  {r.code}"
                print(f"{base} | {format_uwflow_stats(get_uwflow_stats(r.code))}")
        elif args.verbose:
            for r in sorted(online_records, key=lambda r: r.code):
                print(f"  {r.code} - {r.title}")
        else:
            codes = sorted(r.code for r in online_records)
            for i in range(0, len(codes), 8):
                print("  " + "  ".join(codes[i:i + 8]))
    else:
        print("  none")

    if student_profile is not None:
        print(f"\nno conflict ({len(no_conflict_items)}):")
        if no_conflict_items:
            if args.uwflow:
                for item in sorted(no_conflict_items, key=lambda x: str(x["code"])):
                    base = f"  {item['code']} - {item['title']}" if args.verbose else f"  {item['code']}"
                    print(f"{base} | {format_uwflow_stats(get_uwflow_stats(str(item['code'])))}")
            elif args.verbose:
                for item in sorted(no_conflict_items, key=lambda x: str(x["code"])):
                    print(f"  {item['code']} - {item['title']}")
            else:
                codes = sorted(str(item["code"]) for item in no_conflict_items)
                for i in range(0, len(codes), 8):
                    print("  " + "  ".join(codes[i:i + 8]))
        else:
            print("  none")


if __name__ == "__main__":
    main()
