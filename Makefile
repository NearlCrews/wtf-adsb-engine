PYTHON ?= .venv/bin/python
IMAGE ?= wtf-adsb-engine:local
BUILDER_IMAGE ?= $(IMAGE)-build
PLATFORM ?= linux/amd64

.PHONY: check test lint docs build test-wheel image test-image
lint:
	$(PYTHON) -m ruff check src tests tools
	$(PYTHON) -m ruff format --check src tests tools
test:
	$(PYTHON) -B -m unittest discover -s tests -p 'test_*.py'
docs:
	$(PYTHON) tools/check_docs.py
build:
	$(PYTHON) -m build --no-isolation
test-wheel: build
	$(PYTHON) tools/check_wheel.py
check: lint docs test test-wheel
image:
	docker build --force-rm --platform "$(PLATFORM)" --target build -t "$(BUILDER_IMAGE)" .
	docker build --force-rm --platform "$(PLATFORM)" -t "$(IMAGE)" .
test-image:
	$(PYTHON) tools/check_image.py --image "$(IMAGE)" --builder-image "$(BUILDER_IMAGE)"
