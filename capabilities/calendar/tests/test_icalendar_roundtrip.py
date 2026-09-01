from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "calendar", CAPABILITY / "calendar")
    if path.is_file()), CAPABILITY / "bin" / "calendar")


def _load_module():
    name = "calendar_icalendar_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


cal = _load_module()

UTC = timezone.utc
CFG = {"id": "test", "address": "person@example.com", "zone": None,
       "timezone": None, "meeting_url": None, "scope": [], "allow_write": True}
CALENDAR = {"href": "https://dav.example.com/cal/", "name": "Work"}


TIMED = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-1\r\n"
    "DTSTART;TZID=Europe/Berlin:20260902T110000\r\n"
    "DTEND;TZID=Europe/Berlin:20260902T120000\r\n"
    "SUMMARY:Review with the client\\, part two\r\n"
    "LOCATION:Room 3\r\n"
    "DESCRIPTION:First line\\nSecond line\r\n"
    "SEQUENCE:2\r\n"
    "ORGANIZER;CN=Owner:mailto:person@example.com\r\n"
    "ATTENDEE;CN=Guest;PARTSTAT=DECLINED;ROLE=REQ-PARTICIPANT:mailto:guest@exa\r\n"
    " mple.com\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Review\r\n"
    "TRIGGER:-PT15M\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n")

ALL_DAY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
    "UID:event-2\r\nDTSTART;VALUE=DATE:20260902\r\n"
    "SUMMARY:Offsite\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

DURATION_ONLY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
    "UID:event-3\r\nDTSTART:20260902T090000Z\r\nDURATION:PT1H30M\r\n"
    "SUMMARY:Standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def _payload(text, full=True, cfg=CFG):
    component = cal._ics_components(text, "VEVENT")[0]
    return cal._event_payload(cfg, CALENDAR, "https://dav.example.com/cal/x.ics",
                              '"etag-1"', component, full)


class ParsesWhatServersSend(unittest.TestCase):
    def test_a_folded_line_is_one_value(self):
        event = _payload(TIMED)
        self.assertEqual(event["attendees"][0]["email"], "guest@example.com")

    def test_escapes_survive_the_read(self):
        event = _payload(TIMED)
        self.assertEqual(event["title"], "Review with the client, part two")
        self.assertEqual(event["notes"], "First line\nSecond line")

    def test_a_zoned_start_is_emitted_as_utc(self):
        event = _payload(TIMED)
        self.assertEqual(event["startDate"], "2026-09-02T09:00:00+00:00")
        self.assertEqual(event["endDate"], "2026-09-02T10:00:00+00:00")

    def test_an_all_day_event_keeps_bare_dates_and_an_exclusive_end(self):
        event = _payload(ALL_DAY)
        self.assertTrue(event["allDay"])
        self.assertEqual(event["startDate"], "2026-09-02")
        self.assertEqual(event["endDate"], "2026-09-03")

    def test_a_duration_stands_in_for_a_missing_end(self):
        event = _payload(DURATION_ONLY)
        self.assertEqual(event["endDate"], "2026-09-02T10:30:00+00:00")


class BusyIsWhatOccupiesTheSlot(unittest.TestCase):
    def test_a_declined_invitation_frees_the_slot(self):
        invited = dict(CFG, address="guest@example.com")
        self.assertTrue(cal._declined(invited, _payload(TIMED)))

    def test_someone_else_declining_leaves_the_slot_occupied(self):
        self.assertFalse(cal._declined(CFG, _payload(TIMED)))

    def test_an_all_day_event_occupies_whole_days_in_the_connection_zone(self):
        cfg = dict(CFG, timezone="Europe/Berlin",
                   zone=cal._zone("Europe/Berlin"))
        start, end = cal._bounds_utc(_payload(ALL_DAY, cfg=cfg), cfg)
        self.assertEqual(cal._utc_iso(start), "2026-09-01T22:00:00+00:00")
        self.assertEqual(cal._utc_iso(end), "2026-09-02T22:00:00+00:00")

    def test_touching_spans_merge_into_one_block(self):
        first = datetime(2026, 9, 2, 9, tzinfo=UTC)
        spans = [(first, first + timedelta(hours=1)),
                 (first + timedelta(hours=1), first + timedelta(hours=2)),
                 (first + timedelta(hours=4), first + timedelta(hours=5))]
        merged = cal._merge_intervals(spans)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["end"], "2026-09-02T11:00:00+00:00")


class WritesRoundTrip(unittest.TestCase):
    def test_a_built_event_reads_back_as_it_was_written(self):
        body = cal._build_vevent(CFG, "new-1", {
            "title": "Intro; first, meeting", "all_day": False,
            "start": datetime(2026, 9, 2, 9, tzinfo=UTC),
            "end": datetime(2026, 9, 2, 10, tzinfo=UTC),
            "location": "Room 3", "notes": "line one\nline two",
            "url": "https://example.com/room/abc",
            "invitees": ["Guest <guest@example.com>"],
        }, [cal._parse_alarm("-1h30m", CFG)])
        event = _payload(body)
        self.assertEqual(event["title"], "Intro; first, meeting")
        self.assertEqual(event["notes"], "line one\nline two")
        self.assertEqual(event["url"], "https://example.com/room/abc")
        self.assertEqual(event["startDate"], "2026-09-02T09:00:00+00:00")
        self.assertEqual(event["attendees"],
                         [{"name": "Guest", "email": "guest@example.com",
                           "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"}])
        self.assertIn("TRIGGER:-PT1H30M", body)
        self.assertEqual(event["alarms"], 1)

    def test_every_emitted_line_fits_the_protocol_limit(self):
        body = cal._build_vevent(CFG, "new-2", {
            "title": "Ä" * 200, "all_day": False,
            "start": datetime(2026, 9, 2, 9, tzinfo=UTC),
            "end": datetime(2026, 9, 2, 10, tzinfo=UTC),
            "location": None, "notes": None, "url": None, "invitees": [],
        }, [])
        for line in body.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertEqual(_payload(body)["title"], "Ä" * 200)

    def test_an_all_day_event_is_written_with_date_values(self):
        body = cal._build_vevent(CFG, "new-3", {
            "title": "Offsite", "all_day": True,
            "start": datetime(2026, 9, 2).date(),
            "end": datetime(2026, 9, 4).date(),
            "location": None, "notes": None, "url": None, "invitees": [],
        }, [])
        self.assertIn("DTSTART;VALUE=DATE:20260902", body)
        self.assertTrue(_payload(body)["allDay"])


class SubComponentsSurviveAnEdit(unittest.TestCase):
    def test_an_alarm_is_kept_whole_and_apart_from_the_event_properties(self):
        lines = cal._ics_unfold(TIMED)
        begin = lines.index("BEGIN:VEVENT")
        finish = lines.index("END:VEVENT")
        props, subs = cal._split_body(lines[begin + 1:finish])
        self.assertEqual(len(subs), 1)
        self.assertEqual(cal._sub_name(subs[0]), "VALARM")
        self.assertNotIn("TRIGGER:-PT15M", props)
        self.assertIn("DESCRIPTION:Review", subs[0])


def _updated(text, **changes):
    changes.setdefault("text_changes", {})
    changes.setdefault("new_start", None)
    changes.setdefault("new_end", None)
    changes.setdefault("alarms", [])
    changes.setdefault("add_invitees", [])
    existing = _payload(text)
    changes.setdefault("all_day", bool(existing["allDay"]))
    return cal._rebuild_event(CFG, text, existing, **changes)[0]


class AnUpdateChangesOnlyWhatWasAsked(unittest.TestCase):
    def test_an_untouched_property_survives_verbatim(self):
        rebuilt = _updated(TIMED, text_changes={"SUMMARY": "Renamed"})
        event = _payload(rebuilt)
        self.assertEqual(event["title"], "Renamed")
        self.assertEqual(event["location"], "Room 3")
        self.assertEqual(event["notes"], "First line\nSecond line")
        self.assertEqual(len(event["attendees"]), 1)
        self.assertEqual(event["alarms"], 1)

    def test_the_sequence_is_bumped_so_attendees_see_the_revision(self):
        event = _payload(_updated(TIMED, text_changes={"SUMMARY": "Renamed"}))
        self.assertEqual(event["sequence"], 3)

    def test_moving_one_boundary_rewrites_both_properties(self):
        rebuilt = _updated(TIMED,
                           new_start=datetime(2026, 9, 2, 8, tzinfo=UTC))
        event = _payload(rebuilt)
        self.assertEqual(event["startDate"], "2026-09-02T08:00:00+00:00")
        self.assertEqual(event["endDate"], "2026-09-02T10:00:00+00:00")
        self.assertEqual(rebuilt.count("DTSTART"), 1)
        self.assertEqual(rebuilt.count("DTEND"), 1)

    def test_a_start_moved_past_the_kept_end_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            _updated(TIMED, new_start=datetime(2026, 9, 3, 8, tzinfo=UTC))
        self.assertEqual(raised.exception.code, 6)

    def test_turning_an_event_all_day_switches_both_value_types(self):
        rebuilt = _updated(TIMED, all_day=True)
        self.assertIn("DTSTART;VALUE=DATE:20260902", rebuilt)
        self.assertIn("DTEND;VALUE=DATE:20260903", rebuilt)
        self.assertTrue(_payload(rebuilt)["allDay"])

    def test_replacing_the_alarms_leaves_one_set(self):
        rebuilt = _updated(TIMED, alarms=[cal._parse_alarm("-1d", CFG)])
        self.assertEqual(_payload(rebuilt)["alarms"], 1)
        self.assertIn("TRIGGER:-PT24H", rebuilt)
        self.assertNotIn("TRIGGER:-PT15M", rebuilt)

    def test_an_added_invitee_joins_the_existing_ones(self):
        rebuilt = _updated(TIMED, add_invitees=["second@example.com"])
        emails = [a["email"] for a in _payload(rebuilt)["attendees"]]
        self.assertEqual(emails, ["guest@example.com", "second@example.com"])

    def test_a_rebuilt_event_still_fits_the_protocol_limit(self):
        rebuilt = _updated(TIMED, text_changes={"SUMMARY": "Ä" * 200})
        for line in rebuilt.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)


class TheWindowFollowsWhatTheCallerGave(unittest.TestCase):
    def test_a_default_end_is_measured_from_the_given_start(self):
        cfg = dict(CFG, timezone="Europe/Berlin", zone=cal._zone("Europe/Berlin"))
        start, end = cal._window(cfg, "2027-10-01T00:00:00+00:00", None, 7)
        self.assertEqual(cal._utc_iso(end), "2027-10-08T00:00:00+00:00")
        self.assertGreater(end, start)


class TurningAnEventAllDayKeepsItsDay(unittest.TestCase):
    def test_an_evening_event_west_of_greenwich_keeps_its_local_day(self):
        cfg = dict(CFG, timezone="America/New_York",
                   zone=cal._zone("America/New_York"))
        stored = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:evening\r\nDTSTART;TZID=America/New_York:20260902T200000\r\n"
            "DTEND;TZID=America/New_York:20260902T210000\r\n"
            "SUMMARY:Evening\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        existing = _payload(stored, cfg=cfg)
        self.assertEqual(existing["startDate"], "2026-09-03T00:00:00+00:00")
        rebuilt, _ = cal._rebuild_event(
            cfg, stored, existing, text_changes={}, new_start=None,
            new_end=None, all_day=True, alarms=[], add_invitees=[])
        self.assertIn("DTSTART;VALUE=DATE:20260902", rebuilt)
        self.assertIn("DTEND;VALUE=DATE:20260903", rebuilt)


SERIES = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:weekly\r\n"
    "DTSTART:20260902T100000Z\r\nDTEND:20260902T113000Z\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=WE\r\nEXDATE:20260916T100000Z\r\n"
    "SUMMARY:Weekly sync\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:weekly\r\nRECURRENCE-ID:20260909T100000Z\r\n"
    "DTSTART:20260909T140000Z\r\nDTEND:20260909T150000Z\r\n"
    "SUMMARY:Weekly sync (moved)\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n")


def _window(first, last, cfg=CFG, full=True):
    """The occurrences of one stored object inside a window, as the read path
    produces them."""
    return cal._object_events(
        cfg, CALENDAR, "https://dav.example.com/cal/weekly.ics", '"e"',
        cal._ics_components(SERIES, "VEVENT"), full,
        (datetime.fromisoformat(first), datetime.fromisoformat(last)))


class RecurrenceIsExpandedHere(unittest.TestCase):
    """A server may accept a request carrying CalDAV's expand and answer it
    unexpanded, so the occurrences are produced here and the answer does not
    depend on which server replied."""

    def test_every_occurrence_in_the_window_is_one_entry(self):
        events = _window("2026-09-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00")
        starts = [e["startDate"] for e in events]
        self.assertEqual(starts, ["2026-09-02T10:00:00+00:00",
                                  "2026-09-09T14:00:00+00:00",
                                  "2026-09-23T10:00:00+00:00"])

    def test_an_override_replaces_the_occurrence_it_names(self):
        moved = [e for e in _window("2026-09-08T00:00:00+00:00",
                                    "2026-09-10T00:00:00+00:00")]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["title"], "Weekly sync (moved)")
        self.assertEqual(moved[0]["endDate"], "2026-09-09T15:00:00+00:00")

    def test_an_excluded_date_produces_no_occurrence(self):
        events = _window("2026-09-15T00:00:00+00:00", "2026-09-17T00:00:00+00:00")
        self.assertEqual(events, [])

    def test_an_occurrence_starting_before_the_window_still_overlaps_it(self):
        events = _window("2026-09-02T11:00:00+00:00", "2026-09-02T12:00:00+00:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["startDate"], "2026-09-02T10:00:00+00:00")

    def test_a_window_before_the_series_starts_is_empty(self):
        self.assertEqual(
            _window("2026-08-01T00:00:00+00:00", "2026-08-31T00:00:00+00:00"), [])


class InputIsNeverGuessed(unittest.TestCase):
    def test_a_naive_timestamp_without_a_connection_zone_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            cal._parse_input_dt("2026-09-02T10:00", CFG, "--from")
        self.assertEqual(raised.exception.code, 6)

    def test_a_naive_timestamp_resolves_in_the_connection_zone(self):
        cfg = dict(CFG, timezone="Europe/Berlin",
                   zone=cal._zone("Europe/Berlin"))
        parsed = cal._parse_input_dt("2026-09-02T10:00", cfg, "--from")
        self.assertEqual(cal._utc_iso(parsed), "2026-09-02T08:00:00+00:00")

    def test_an_offset_is_taken_as_given(self):
        parsed = cal._parse_input_dt("2026-09-02T10:00:00+03:00", CFG, "--from")
        self.assertEqual(cal._utc_iso(parsed), "2026-09-02T07:00:00+00:00")


class NothingOutsideTheScopeIsReachable(unittest.TestCase):
    """A located href decides which host receives the credential and which
    collection is written to, so containment is checked, never approximated."""

    def test_a_foreign_host_is_never_inside_a_calendar(self):
        self.assertFalse(cal._within(
            "https://elsewhere.example.net/cal/x.ics", CALENDAR["href"]))

    def test_a_sibling_collection_is_not_inside_this_one(self):
        self.assertFalse(cal._within(
            "https://dav.example.com/calendar/x.ics", CALENDAR["href"]))
        self.assertTrue(cal._within(
            "https://dav.example.com/cal/x.ics", CALENDAR["href"]))

    def test_a_name_prefix_does_not_make_a_calendar_the_same_one(self):
        work = {"href": "https://dav.example.com/cal/work/", "name": "Work"}
        self.assertFalse(cal._within(
            "https://dav.example.com/cal/workshop/evt.ics", work["href"]))

    def test_an_href_naming_another_host_matches_no_calendar(self):
        self.assertFalse(cal._same_calendar(
            "https://elsewhere.example.net/cal/", CALENDAR))


class AUidIsVerifiedAgainstWhatWasAsked(unittest.TestCase):
    """A CalDAV text-match is a substring match, so the server's answer is a
    candidate and the uid is what identifies the event."""

    def test_a_longer_uid_containing_the_query_is_not_the_event(self):
        component = cal._ics_components(TIMED.replace("UID:event-1",
                                                      "UID:event-1-2"), "VEVENT")
        self.assertNotEqual(cal._text(component[0], "UID"), "event-1")


class AZoneIsNeverInventedOnTheReadPath(unittest.TestCase):
    def test_a_prefixed_tzid_resolves_to_the_iana_name_inside_it(self):
        zone = cal._tzid_zone("/freeassociation.sourceforge.net/Europe/Berlin")
        self.assertIsNotNone(zone)

    def test_a_zone_this_host_cannot_resolve_is_reported(self):
        text = TIMED.replace("DTSTART;TZID=Europe/Berlin:",
                             'DTSTART;TZID="W. Europe Standard Time":')
        self.assertEqual(_payload(text)["zoneFallback"],
                         "W. Europe Standard Time")

    def test_a_resolved_zone_is_reported_as_no_fallback(self):
        self.assertIsNone(_payload(TIMED)["zoneFallback"])


class MalformedDataIsAFindingNotACrash(unittest.TestCase):
    def test_an_unreadable_sequence_reads_as_zero(self):
        text = TIMED.replace("SEQUENCE:2", "SEQUENCE:0;X")
        self.assertEqual(_payload(text)["sequence"], 0)


class NonTextValuesCannotInjectProperties(unittest.TestCase):
    def test_a_line_break_in_a_url_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            cal._clean("https://x/\r\nSTATUS:CANCELLED", "--url", drop="")
        self.assertEqual(raised.exception.code, 6)

    def test_a_line_break_in_an_invitee_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            cal._attendee_line("A\r\nATTENDEE:mailto:x@example.com")
        self.assertEqual(raised.exception.code, 6)

    def test_a_quote_cannot_escape_a_parameter(self):
        line = cal._attendee_line('A"B <x@example.com>')
        self.assertEqual(line.count('"'), 2)


class ScopeIsMatchedByNameOrHref(unittest.TestCase):
    def test_a_display_name_matches_case_insensitively(self):
        self.assertTrue(cal._same_calendar("work", CALENDAR))

    def test_an_href_matches_by_path(self):
        self.assertTrue(cal._same_calendar("/cal/", CALENDAR))
        self.assertTrue(cal._same_calendar(CALENDAR["href"], CALENDAR))

    def test_an_unrelated_token_matches_nothing(self):
        self.assertFalse(cal._same_calendar("Personal", CALENDAR))


if __name__ == "__main__":
    unittest.main()
