# Node provider fixtures

These sanitized response recordings reproduce the Coolify 4.3.23 response shapes from the accepted node experiment and the C2 deploy fixture: resource UUIDs and deployment UUIDs remain distinct. Values are synthetic. The fake provider records the CLI requests separately, including bulk input in a temporary test directory, so tests can prove secrets do not enter argv or command output. Rollback and wait fixtures model the accepted C3/C2 interfaces; this suite does not claim those upstream verbs are installed.
