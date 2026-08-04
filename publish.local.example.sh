# Copy this file to publish.local.sh and add your FTP credentials.
# Never commit publish.local.sh.

FTP_HOST="ftp.example.com"
FTP_USER="your-username"
FTP_PASS="your-password"
FTP_REMOTE_DIR="/"

# Optional: only needed if a model role in ingest.py uses the "openrouter"
# provider. Must be exported so ingest.py (a child process) inherits it.
# export OPENROUTER_API_KEY="sk-or-..."

# Optional: auto-announce + comments. When set, publish.sh posts a link to each
# newly published post and wires the replies in as that article's comments.
# Replies from both networks below are blended into ONE thread on the page, so
# you can enable either, both, or neither - the build and deploy work regardless.

# Mastodon. The token needs the write:statuses and write:bookmarks scopes
# (create one at <your instance> -> Preferences -> Development -> New application).
# MASTODON_SERVER="mastodon.social"   # your instance host, no https://
# MASTODON_TOKEN="your-access-token"  # write:statuses + write:bookmarks
# MASTODON_ID="yourhandle"            # your @handle (no @), used for links/logs

# Bluesky. Use an APP PASSWORD, not your account password - create one at
# bsky.app -> Settings -> App Passwords. An app password can post on your
# behalf but cannot delete or migrate the account.
# BLUESKY_HANDLE="you.bsky.social"
# BLUESKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
# BLUESKY_PDS="bsky.social"           # only if your account is self-hosted
