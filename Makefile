.PHONY: build test master agent tidy release-linux deb

build: master agent

master:
	go build -o bin/ip-sentinel-master ./cmd/master

agent:
	go build -o bin/ip-sentinel-agent ./cmd/agent

test:
	go test ./...

tidy:
	GOPROXY=https://goproxy.cn,direct go mod tidy

release-linux: deb

deb:
	bash scripts/build-deb.sh

fetch-trends:
	python scripts/fetch_trends.py

fetch-trust-urls:
	python scripts/fetch_trust_urls.py

ua-factory:
	python scripts/ua_generator.py
