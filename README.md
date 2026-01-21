# AI News Agent

`agent_core/` -- the agent logic
`services/` -- services needed
`app.py` -- run the app

# Setup

* install python, pip, screen
* create venv, activate
* copy the code in the VM
* install dependencies in requirements.txt, and also `playwright install --with-deps chromium`
* start a screen session, run `python3 app.py`, detach

# Run Agent

`python3 app.py --now` run the agent right away only once
`python3 app.py` start the scheduler and run agent on schedule (running forever)