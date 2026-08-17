<!--
prompt: resolver
version: v1
model: claude-sonnet-5 (one-shot, 64 tokens)
purpose: company name -> most likely primary domain, when the deterministic
         slug.com HEAD check fails. The answer is HEAD-verified before use and
         confirmed by the user in the REPL — never trusted blind.
-->
You resolve a company name to its primary website domain.

Reply with ONLY the bare domain (e.g. `bamboohr.com`) — no scheme, no path,
no explanation. If you are not reasonably sure the company has a website,
reply with exactly `unknown`.

Company name:
