
## Todo
seem to be unable to delete Firestore /checkpoints data
how do we setup a TTL or regular cleanup?
some data has no created_as so unable to delete by timestamp
Firestore TTL md document is wrong about TTL setup - need to fix


# Add some kind of cookie or localstorage to persist sessions
Store sessionid in the localstore and use it when woken up.
Only create a new session when logged out/in or on /new command