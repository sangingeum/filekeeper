# filekeeper-mcp (not yet implemented)

This directory reserves the separate distribution that will ship the MCP server.
It exists now so the two-distribution layout is fixed: the `filekeeper`
distribution carries Core and the CLI with no third-party dependencies, and this
one carries the MCP SDK plus the server.

Nothing here is implemented in the 0.1 series, and no server code is shipped. The
package is not published. When it lands, it will depend on `filekeeper>=0.1,<0.2`
and expose the `filekeeper-mcp` script over STDIO.
