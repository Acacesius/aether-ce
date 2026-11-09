.PHONY: up down test scan demo
up:    ; scripts/quickstart.sh up
down:  ; scripts/quickstart.sh down
demo:  ; scripts/demo_flow.sh
test:  ; python3 -m unittest discover -s tests
scan:  ; python3 tools/ce_scan.py .
