Require agentgrep

Output docs/_static/demos/vhs/agentgrep-json-origin.gif
Output docs/_static/demos/vhs/agentgrep-json-origin.mp4
Output docs/_static/demos/vhs/agentgrep-json-origin.webm

Set FontSize 18
Set Width 1580
Set Height 300
Source docs/_static/demos/vhs/_preamble.tape

Sleep 1s
Type "agentgrep search TODO --scope conversations --json | jq -r '.results[] | [.agent, .model, .origin.cwd, .origin.branch] | @tsv'"
Enter
Wait
Sleep 4s
