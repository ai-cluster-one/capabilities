# Moving a memory folder into the store

How to carry a project's existing memory folder into the store without losing an entry or carrying one twice.

## Read the dry run first

Run adopt without applying, naming the project roots you mean, or with none to take every project root a machine service on this machine lists. The answer shows, for every entry it found, the title it read, the date it gave the entry and where that date came from, and whether it will be carried or skipped and why; it also lists every file it did not read. Nothing is written. Read it before applying: a folder written in a shape the reader does not expect shows up here as odd titles or skipped sections, and is easier to tidy now than to supersede later.

## Check the zone the dates are read in

A memory folder's file dates and section times carry no zone of their own, so adopt reads them in the machine's local zone, which is the zone a writer on the same machine stamped them in. When the folder was written on a machine in another zone, pass that machine's IANA zone with `--timezone`, such as `Europe/Berlin`; each date then takes the offset that zone had on it. Every entry's `dated_from` names the zone it was read in, so the dry run shows whether the moments are the ones the folder meant.

## Decide what is pinned

A pinned entry stays out of every grooming pass, so pin the decisions the project's owner made whose words must stay as they were written. When their titles share a recognisable form, pass a pattern that matches them and they are pinned as they are carried; otherwise pin them one by one afterwards. Every session reaches an entry through search, so a decision every session must hold is promoted into the project's doctrine before or after the move.

## Apply

Applying enables memory for each project that does not already decide for itself, opts it in to the machine service - a project in the store is placed on this node in mode shared, as `capabilities place memory --node <this node> --mode shared` places it, and the node manager puts it on the machine list; a project in files mode is answered as not joined, with why - carries every entry with its original date, and renders its block. A project that disables memory is left alone. Running it again, here or on another machine that holds the same project, carries nothing twice and reports each entry it finds already carried.

## Afterwards

Check the project with `memory status` and `memory doctor`, and read the block it now carries. The folder itself is left exactly as it was; removing it, and whatever in the project still writes to it, is the project's own change to make once its memory reads from the store on every machine it runs on.
