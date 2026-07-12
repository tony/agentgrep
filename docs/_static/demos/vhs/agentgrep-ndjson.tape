Require agentgrep

Output docs/_static/demos/vhs/agentgrep-ndjson.gif
Output docs/_static/demos/vhs/agentgrep-ndjson.mp4
Output docs/_static/demos/vhs/agentgrep-ndjson.webm

Set FontSize 18
Set Width 1220
Set Height 300
Source docs/_static/demos/vhs/_preamble.tape

Sleep 1s
Type "agentgrep search 'cache OR retry' --ndjson | jq -c '{agent, score, text}'"
Enter
Wait
Sleep 4s
