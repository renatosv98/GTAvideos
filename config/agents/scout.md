## Owner rules (set by the owner; the manager never changes this section)
- Only Spanish creators with permission (the permitted list in your prompt, from config/creators.yaml). Only recordings with GTA.

## Manager instructions
- Write stream numbers as S#n, clip numbers as #n.
- Then one line per permitted creator: 'no new VOD' / 'VOD not GTA' / 'processed' / 'skipped: backlog' + latest VOD title, date, GTA 6 yes/no.
- Queue: ~78 approved clips to 27 Nov 00:30; Auronplay only 3 (#102, #103, #106). Process Auronplay GTA 6 VODs first, then IlloJuan, the day they end, before any Rubius VOD.
- Rubius GTA 6: while ≥30 clips are queued, max 1 Rubius VOD per day (all its parts count as one); log 'skipped: backlog' for the rest.
- Multi-part VODs: log each part's source range (hh:mm–hh:mm); each part starts where the last ended; never re-process a range. If a part errors, name the part.
- Skip older-GTA VODs while any GTA 6 VOD waits or ≥6 clips are queued; log 'skipped: backlog'. Tag footage 'GTA 6', 'GTA V', 'GTA IV' or 'GTA Vice City (classic)'.
- Never skip a permitted creator's VOD on title alone: check chapters, category changes, keywords (GTA, VI, Rockstar, Lucía, Jason, Leonida).
- Titles with codes/drops ('CÓDIGOS', '!DROPS ON'): log 'codes/drops on stream'.
- For every skip log creator, title, date, reason and what you checked.
- Stuck: S#34, S#36 (IlloJuan) 'new' 18 h+. List them at the top as 'stuck: S#34, S#36 – see autopilot.log'; never re-download a range
