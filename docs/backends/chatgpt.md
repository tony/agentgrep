(backend-chatgpt)=

# ChatGPT desktop

agentgrep searches ChatGPT desktop's conversation titles on Windows. Neither
app keeps message text on disk in a form another program can read: the
Windows app caches only the conversation list, and the macOS app encrypts its
conversation files with a key that only OpenAI's own code can read.

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

The list counts as conversation content, so a conversation-scope search
reads it:

```console
$ agentgrep search --agent chatgpt --scope conversations --exhaustive deploy
```

The cache holds the conversations the sidebar showed when the app last ran;
older conversations and every message body stay on OpenAI's servers.

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
