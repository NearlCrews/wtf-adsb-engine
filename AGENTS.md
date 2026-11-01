# Setting up WTF ADS-B Engine with an AI assistant

This file is for an AI assistant that a user has opened in this repository.
When the user asks you to set the engine up, follow this protocol. It
configures the Docker Compose deployment in `deploy/compose.yaml`, checks it,
and starts it. `CLAUDE.md` holds the same text for harnesses that read that
name.

## Setup protocol

1. Read [docs/setup-interview.md](docs/setup-interview.md). It lists every
   question with its key, type, default, validation rule, and reason.
   `python3 tools/setup.py plan` prints the same list as JSON.
2. Confirm the prerequisites: Linux on x86-64, Docker with the Compose plugin,
   Python 3.11 or newer for the setup tool, and a receiver whose
   `aircraft.json` the Docker host can reach. If the image does not exist yet,
   `make image` builds `wtf-adsb-engine:local`.
3. Ask the user to name a private directory outside this repository for the
   configuration, such as `/srv/wtf-adsb/config`. It holds the answers and the
   generated files, and Compose mounts it read-only at `/config`.
4. Ask the interview groups in order: Receiver, Directories, Providers,
   Identity, and Runtime. Send one message per group. Show each question's
   default and accept "defaults" as an answer. Validate each answer against
   the rule in the document and ask again when one fails. Tell the user that
   the Receiver, Providers, and Identity answers stay fixed for the life of
   the state directory.
5. For a provider key, ask only for the path of a key file the user has
   already created. Never ask for, accept, or repeat the key itself. If the
   user pastes a key, tell them not to, do not store or echo it, and ask them
   to put it in a file with mode 0600 themselves.
6. Write the answers as one JSON object to `answers.json` in the private
   directory. Create the directory with mode 0700 first if it does not exist.
   If its parent needs root, such as `/srv`, offer
   `sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/wtf-adsb` first.
7. Run `python3 tools/setup.py configure --answers DIR/answers.json --output DIR`,
   where `DIR` is the private directory. If `uid` and `gid` differ from the
   user's own account, run it with `sudo` so it can give the directories to
   them. Read every warning in the result to the user.
8. Run `python3 tools/setup.py check --output DIR`. Fix each failed check with
   the user, then run check again until `ok` is true.
9. Run `python3 tools/setup.py deploy --output DIR`. It starts the services
   and waits up to 120 seconds for the engine's health command.
10. Report the health JSON from deploy to the user. Explain that readiness can
    take several minutes longer, because the first export must finish.

Every subcommand prints one JSON object and exits nonzero on failure. Rerunning
configure with the same answers changes nothing. `deploy --print-only` shows
the Compose commands without running them.

## Upkeep to tell the user

In these commands, `DIR` is the private directory.

- Upgrade: update the checkout, then rebuild with `make image` or pull the
  published tag named in `image`. Run configure, check, and deploy again;
  deploy reruns `docker compose up -d`. The state and feed directories carry
  over.
- Health: `docker compose --env-file DIR/.env -f deploy/compose.yaml exec -T engine wtf-adsb-engine health --config /config/receiver.toml --readiness`
- Backup: stop the engine with `docker compose --env-file DIR/.env -f deploy/compose.yaml stop`,
  copy the state, configuration, reference, and feed directories with their
  permissions into a private backup location, then start it again with
  `up -d`. The state directory holds the receiver journal and the delivery
  outbox. See [operations](docs/operations.md#state-backups-and-upgrades).

## Never do these

- Commit, paste, or upload the answers file, `receiver.toml`, `.env`, any key
  file, or anything from the private directories.
- Print, log, or read the contents of a key file.
- Turn on `flightaware` or any other paid provider without an explicit yes
  from the user in this conversation.
- Change a provider budget without asking, or raise one beyond what the user
  stated.
- Publish the viewer's port 8080 on a public interface, or expose the feed,
  state, or receiver host to the internet.
- Write configuration inside this repository.

## Working on the code

If the user wants to change the engine rather than run it, read
[DEVELOPMENT.md](DEVELOPMENT.md). It covers the checkout setup, `make check`,
image builds, plugins, releases, and the rules for changes.
