# Idea: an OBP-API endpoint that traces what an operation id calls

## Where we are

The source review starts from an endpoint's operation id. Sentinel finds the file the endpoint is defined
in, and the analyst follows the code from there by reading it. What it read is recorded per file, by git
content, in `sentinel-source.db`, so unchanged files are not read again.

The weak point is the trace itself. It is the analyst's reading, so it can stop early, miss code that runs
around the handler (middleware, authorisation checks) and miss dynamic code. The list of files is what the
analyst says it read, and nothing checks it. Static guesses do not fix this: which connector is active, or
matching names like `NewStyle.function.*` in the text, says little once dynamic code is involved.

## The idea

Add an endpoint to OBP-API that returns, for an operation id, the functions and source files that the
operation calls on that instance. It would come from the running code (Scala reflection, or the bytecode
of the loaded classes), not from reading the source.

- **Correct per instance.** Each instance answers for the code it runs, with its own props, its own
  connector and its own dynamic code, so the trace matches what that instance really does.
- **A record that can be checked.** Sentinel would know the full set of files behind each operation id. It
  could tell which of them the analyst has reviewed and which it has not, and bring back an endpoint
  when any of its files changes.
- **Less to read twice.** Files many endpoints share (NewStyle, APIUtil, the connector) would be found
  as shared from the trace, and reviewed once.

## Open questions

- How far to follow: through the connector into mappers and SQL, into library code, into dynamic
  endpoints and entities.
- Whether reflection on the running JVM is enough, or the bytecode of the loaded classes has to be read
  (for example with ASM) to see the calls a method makes, and how to map classes back to source files.
- Cost: compute traces once at start-up and cache them, or on request.
- Who may call it (a Role, held as a Scope by a code-review service such as Sentinel), and what it must
  not reveal.
