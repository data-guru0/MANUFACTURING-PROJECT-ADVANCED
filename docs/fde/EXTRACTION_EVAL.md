# Extraction evaluation (S4)

_Generated 2026-11-23 16:58 UTC by `python -m apex_agent.documents.evaluate` from the live `extractions` table, the traffic labels and the L3 ground truth (read only by this evaluation)._

**17 RFQ emails, 24 parts.** A field is scored only where it can be known: a part named in the email without a drawing (a repeat) has no size to read, so only its material and quantities count.

**2 of 24 parts** have at least one field flagged for a person to confirm (low confidence, a conflict between sources, or a size read from a scan).

## Per field

| field | all | DXF | no drawing sent | scan | vector PDF |
|---|---|---|---|---|---|
| found | **100% (24/24)** | 100% (5/5) | 100% (2/2) | 100% (2/2) | 100% (15/15) |
| material | **96% (23/24)** | 100% (5/5) | 50% (1/2) | 100% (2/2) | 100% (15/15) |
| envelope | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| holes | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| threads | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| tolerance | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| ra | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| general_tolerance | **100% (22/22)** | 100% (5/5) | - | 100% (2/2) | 100% (15/15) |
| quantities | **100% (24/24)** | 100% (5/5) | 100% (2/2) | 100% (2/2) | 100% (15/15) |

Rules: size = length, width, height each within 1 %; holes = count and diameter (2 %); tolerance = the tightest one when the drawing shows one tighter than 0.1 mm; inch drawings are converted to mm; an inch thread ("5/16-18 UNC") is correct when it is the drawing's thread.

## The email (any language)

| requirement | correct |
|---|---|
| requested_date | 100% (17/17) |
| co2_requested | 100% (17/17) |
| export_control | 100% (17/17) |
| fai | 100% (17/17) |
| ppap | 100% (17/17) |
| incoterms | 100% (17/17) |
| language | 100% (17/17) |
| route | 100% (17/17) |

`route` = an export-control marker sent the RFQ to a senior (OPA `apex/extraction/route`).

## Misses (first 40)

- RFQ-011 D-20013-A (no drawing sent, en): **material**

## NIST MBE PMI test drawings (no ground truth: what the vector-PDF reader finds)

Real third-party drawings (inch, ASME Y14.5 GD&T), very unlike Apex's. Test drawings courtesy of NIST.

Values are as written when the drawing does not state its units (NIST's notes don't).

| file | read by | units | GD&T | tightest tolerance | hole note | thread | Ra | size found |
|---|---|---|---|---|---|---|---|---|
| nist_ctc_01.pdf | OCR + vision | not stated | - | 0.5 | 2 x Ø20.0 | - | - | no |
| nist_ctc_02.pdf | OCR + vision | not stated | - | 0.15 | 2 x Ø100.0 | - | - | no |
| nist_ctc_03.pdf | OCR + vision | not stated | - | 0.005 | 2 x Ø0.44 | - | - | no |
| nist_ctc_04.pdf | OCR + vision | not stated | - | 0.1 | 30 x Ø14.0 | M12 | - | no |
| nist_ctc_05.pdf | OCR + vision | not stated | - | 0.001 | - | - | - | no |
| nist_ftc_06.pdf | vector text | not stated | - | - | - | - | - | no |
| nist_ftc_07.pdf | vector text | not stated | yes | 0.001 | 2 x Ø0.25 | - | - | yes |
| nist_ftc_08.pdf | vector text | not stated | - | - | - | - | - | no |
| nist_ftc_09.pdf | vector text | not stated | - | - | 2 x Ø0.32 | - | - | no |
| nist_ftc_10.pdf | vector text | not stated | - | - | - | - | - | no |
| nist_ftc_11.pdf | OCR + vision | not stated | - | 0.04 | 1 x Ø32.0 | - | - | no |

Tolerances or GD&T found on **7 of 11** NIST drawings, a size on 1 of 11. The readers were built around Apex's drawing conventions; NIST's (PMI drawn as graphics, no title-block labels, many views, no stated units) show how far that generalises. Anything not read with confidence goes to a person.
