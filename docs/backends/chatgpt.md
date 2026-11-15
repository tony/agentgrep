(backend-chatgpt)=

# ChatGPT desktop

agentgrep searches ChatGPT desktop's conversation titles and scheduled-task
prompts on Windows. Neither app keeps message text on disk in a form another
program can read: the Windows app caches only the conversation list and the
scheduled-task list, and the macOS app encrypts its conversation files with a
key that only OpenAI's own code can read.

Base paths: `%LOCALAPPDATA%/Packages/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/LocalCache/Roaming/ChatGPT`
(Windows, reached from WSL through each Windows profile) and
`~/Library/Application Support/com.openai.chat` (macOS).

`observed_version`: ChatGPT desktop (observed 2026-11-14).

## What is searched

{storage:storeref}`chatgpt.conversation_list` reads the Windows app's
Chromium Local Storage, a LevelDB directory, without a LevelDB library and
without taking the app's lock. The app caches the first page of its
conversation list there (about 28 conversations, plus project lists), so
agentgrep finds each cached conversation by title. Each title becomes one
record whose session id is the ChatGPT conversation id, with the creation
time, project id, and archive, star, and temporary-chat flags in its
metadata. Only the conversation-list keys are decoded; the store's session
data and secrets are never read.

{storage:storeref}`chatgpt.http_cache` reads the app's Chromium HTTP cache,
in Chromium's blockfile format, the same way: without Chromium and without
its lock. Three cached responses are read: the conversation list, each
project's conversation list (project conversations the Local Storage list
leaves out), and the scheduled-task list. Conversations become title records
as above, and each scheduled task with a prompt becomes a user prompt record
holding the text the user scheduled; a task's model output is not read. Every
other cached response, account and session data among them, is never read
past its URL. The bodies are Brotli-compressed, which the `brotli` package
decodes.

Both stores count as conversation content, so a conversation-scope or
broad-scope search reads them (a conversation scope runs at exhaustive effort
unless told otherwise; targeted effort does not read the list):

```console
$ agentgrep search --agent chatgpt --scope conversations deploy
```

A result looks like this in `--json` output (values are illustrative):

```json
{
  "kind": "history",
  "agent": "chatgpt",
  "store": "chatgpt.conversation_list",
  "path": "%LOCALAPPDATA%/Packages/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/LocalCache/Roaming/ChatGPT/Local Storage/leveldb/CURRENT",
  "text": "Plan the widget launch",
  "title": "Plan the widget launch",
  "role": "assistant",
  "timestamp": "2026-09-25T10:15:02.123456Z",
  "session_id": "68d1c0aa-0000-0000-0000-000000000000",
  "metadata": {"created_at": "2026-09-12T08:00:00.000000Z", "starred": true}
}
```

The `role` is `assistant` because ChatGPT writes a conversation's title
itself; the cache does not mark a title the user renamed, so `role:user`
leaves the titles out.

The printed path works as a `path:` query, so the ChatGPT titles can be
picked out of a broader search:

```console
$ agentgrep search --scope all 'path:%LOCALAPPDATA%/Packages/OpenAI.ChatGPT* deploy'
```

The cache holds the conversations the sidebar showed when the app last ran;
older conversations and every message body stay on OpenAI's servers. Because
it is a cache, it can still list a conversation deleted or moved off the first
page since the app last refreshed: agentgrep keeps every conversation any
cached snapshot lists, newest title per conversation.

## Stores

```{storage:agent} chatgpt
```

## What is not searched

{storage:storeref}`chatgpt.conversations` and
{storage:storeref}`chatgpt.drafts` are the macOS app's
`conversations-v3-<uuid>/<UUID>.data` and `drafts-v2-<uuid>/<UUID>.data`
files. The app (bundle `com.openai.chat`) seals each file with CryptoKit
AES-GCM under a 256-bit key and stores a property list inside. The key is a
keychain item in OpenAI's team access group in the data protection
keychain, which macOS opens only to code signed with that entitlement
([TN3137: On Mac keychain APIs and implementations](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains)),
so neither agentgrep nor the `security` tool can decrypt the files. These
findings come from the app's binary and Apple's documentation, not from a
Mac.

{storage:storeref}`chatgpt.web_profile` is the rest of the Windows profile.
Decoded, its IndexedDB `ConversationsDatabase` holds no records. Its HTTP
cache holds conversation-list responses, which repeat the Local Storage list,
and scheduled-task records, whose prompts are the one piece of typed text in
the profile, but no conversation body. The cache entries are Brotli-compressed
and evicted as the app runs, and agentgrep ships no Brotli decoder, so the
cache is listed, not read.

`find` and the `agentgrep://sources` resource list these stores so a storage
audit shows that the files exist; `find --json` marks each with
`"coverage": "catalog_only"` in its metadata, and search never opens them.
