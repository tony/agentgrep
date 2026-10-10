Require agentgrep

Output docs/_static/demos/vhs/agentgrep-json.gif
Output docs/_static/demos/vhs/agentgrep-json.mp4
Output docs/_static/demos/vhs/agentgrep-json.webm

Set FontSize 18
Set Width 1200
Set Height 300
Source docs/_static/demos/vhs/_preamble.tape

Sleep 1s
Type "agentgrep search deploy --json | jq -r '.results[] | [.agent, .text] | @tsv'"
Enter
Wait
Sleep 4s
