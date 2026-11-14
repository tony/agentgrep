(backend-chatgpt)=

# ChatGPT desktop (unsupported)

ChatGPT desktop is catalogued so readers can see that agentgrep looked for
it and why it does not appear in search. Neither app keeps conversations in
a form agentgrep can read.

Base paths: `~/Library/Application Support/com.openai.chat` (macOS) and
`%LOCALAPPDATA%/Packages/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/LocalCache/Roaming/ChatGPT`
(Windows).

`observed_version`: ChatGPT desktop (observed 2026-11-14).

```{warning}
ChatGPT desktop is **documented but unsupported**. The macOS app writes one
`.data` file per conversation that looks encrypted, and the Windows app is a
Chromium profile with no conversation files. agentgrep catalogues the
locations and lists the files for inventory but does **not** cover ChatGPT
desktop: it is excluded from `--agent` selection and from search.
```

## Stores

```{storage:agent} chatgpt
```

All rows are catalog-only, so `find` and search never open them. The MCP
{tool}`list_sources` tool with `include_non_default`, and
{tool}`filter_sources` with `coverage_filter="catalog_only"`, list the files
that exist on the machine, from macOS or from Windows (natively or through
WSL), described by size and modification time only.

## Record schemas

### macOS conversations and drafts

{storage:storeref}`chatgpt.conversations` covers
`conversations-v3-<uuid>/<UUID>.data`, one file per conversation, from 898 B
to 349 KB. None of 30 sampled files parsed as JSON, UTF-8, zlib, raw deflate,
gzip, bzip2, or lzma. The entropy of each file's first 64 KiB was 7.76 to
8.00 bits per byte, and every file started with different bytes, which fits
authenticated encryption. {storage:storeref}`chatgpt.drafts` covers the
unsent drafts in `drafts-v2-<uuid>/<UUID>.data`, which are as opaque.

### Windows profile

{storage:storeref}`chatgpt.web_profile` covers the Windows app, an Electron
shell around chatgpt.com installed as the MSIX package
`OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0`. Windows redirects the package's
`%APPDATA%` writes to its `LocalCache/Roaming` directory, so from WSL the
data is only under `%LOCALAPPDATA%/Packages`. The directory is a Chromium
profile: `IndexedDB`, `Local Storage`, `Session Storage`, `Preferences`, and
a two-byte `config.json`. Searching the IndexedDB files as bytes for
`"author"` or `conversation_id` found nothing, so the conversations are held
on OpenAI's servers.
