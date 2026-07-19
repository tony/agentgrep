Require agentgrep

Output docs/_static/demos/vhs/agentgrep-find-json.gif
Output docs/_static/demos/vhs/agentgrep-find-json.mp4
Output docs/_static/demos/vhs/agentgrep-find-json.webm

Set FontSize 18
Set Width 1200
Set Height 300
Source docs/_static/demos/vhs/_preamble.tape

Sleep 1s
Type "agentgrep find --json | jq -r '.results[].agent' | sort | uniq -c"
Enter
Wait
Sleep 4s
