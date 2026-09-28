"""Music text-search subsystem (song name / artist -> numbered results).

See app/services/search/base.py for the provider-agnostic interface and the
`SearchResult` shape every provider returns, and itunes_client.py for the
only live provider (Apple iTunes Search API — free, no key/auth).
"""
