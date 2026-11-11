"""The five agents of the clip machine.

1 transcriber: stream -> full transcript + moment metrics (report.json)
2 selector:    report + playbook + feedback -> ranked clip plans (Claude)
3 producer:    clip plans -> finished vertical shorts
4 publisher:   approved shorts -> YouTube / TikTok / Instagram on a flexible schedule
5 analyst:     published shorts' stats -> playbook updates for the selector (Claude)
"""
