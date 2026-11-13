# 00 · Overview

## One-liner

**KraftSync turns the site chat into a live construction schedule: every "done", "late" or "we already started" message updates a dependency graph, and everyone whose work moves hears about it in minutes, not at next week's site meeting.**

## Problem

A construction schedule is a dependency graph (drywall → inspection → boarding → screed → …). On real sites:
- The schedule lives in MS Project / Primavera / Excel and is updated weekly at best.
- Reality lives in WhatsApp groups, phone calls and email — dozens of messages a day.
- Nobody connects the two. A plumber's "pre-wall elements arrive Thursday" doesn't reach the drywaller who plans to close that wall Wednesday, or the inspector, or the site manager whose handover date just moved.

Cost (cite these in the pitch):
- Miscommunication and poor project data cause ~48% of all rework on US jobsites, ≈ $31.3 bn in 2018 (FMI / PlanGrid, *Construction Disconnected*).
- Site teams spend ~35% of their time (14+ h/week) on non-productive work: looking for information, resolving conflicts, fixing mistakes (same study).
- UK: avoidable errors ≈ 21% of project value (Get It Right Initiative).
- Brief context: Talona cites $1.85 tn lost to bad data and miscommunication in 2020.

## Who uses it

| Person | Today | With KraftSync |
|---|---|---|
| Trade foreman (electrician, plumber, …) | Texts the group | Texts the group. Nothing to learn. Gets told when *their* start date moves. |
| Site manager (Bauleiter) | Reconstructs the week from chats | Live graph, discrepancy feed, only gets pinged for decisions |
| Inspector (ÖBA) | Called last minute | Notified when a hold point is ready — or violated |

## What it does (MVP)

1. **Plan → graph.** Tasks, dependencies, people, locations from CSV/JSON (MS Project/P6 import as stretch).
2. **Chat → updates.** Telegram (live) + WhatsApp/email (as available). Sender resolved from the persons list.
3. **Interpret.** The engine maps each message to a task and an update kind (started / completed / delayed / blocked / modified / question), with confidence. Unsure → asks back.
4. **Detect.** The graph recomputes dates (CPM) and raises discrepancies: delay with downstream impact, out-of-sequence work, hold-point violation, scope change, conflicting reports, silent overdue.
5. **Resolve.** The policy decides who needs to know and what to ask; integrations send it.
6. **Log & show.** Every change is an event; the UI shows the graph, the critical path, and the discrepancy feed.

## Demo story (data in `data/demo/`)

Project: fit-out of the 2nd floor, apartments Top 5 and Top 6, 26 tasks, 9 people. Story date: Mon 9 Nov 2026.

1. Electrician: "Top 6 Elektro fertig" → task done, graph turns green. Nothing else happens (correct!).
2. Plumber: "pre-wall elements missing, Thursday + 1 day" → delay → KraftSync shows the ripple: inspection, drywall boarding, screed … and the **floor handover date moves**. Drywaller and inspector get a message with their new dates; site manager gets "handover +3 days, decide".
3. Electrician: "client wants 2 more sockets" → scope change logged with who/when → site manager asked to confirm (and it's billable).
4. Drywaller: "already started boarding Top 6 bathroom" → **hold-point violation**: inspection not done → critical escalation to site manager + inspector *within a minute*, not after the wall is closed.
5. Electrician: "fertig" (ambiguous) → KraftSync asks "Which task — …?" instead of guessing.

Closing line: *"Nobody changed how they work. The schedule just stopped lying."*

## Judging alignment (Talona)

| Talona "what wins" | KraftSync |
|---|---|
| Connect to what the site already uses | Lives in the chat groups crews already use; imports existing schedules |
| One live record of what changed and who decided | Append-only event log; every graph change has a source message and actor |
| Flag issues as they happen | Discrepancies raised on the message that causes them |
| Decisions go to the superintendent | Policy escalates decisions to the site manager; FYIs go to affected trades |

## Explicitly out of scope

Cost/billing calculation, resource levelling, BIM, full working-day calendars, photo understanding (stretch only), mobile app.
