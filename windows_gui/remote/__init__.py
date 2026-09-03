"""Remote capability package: transport-side primitives only.

Remote is a transport layer, never a permission layer. This package must
never gain execute, consume, or complete abilities, and must never touch
TaskCenter verified context.
"""
