package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"code-migration-agent/go-analyzer/analyzer"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("analyzer", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: analyzer <repository-path>")
	}
	if err := flags.Parse(args); err != nil {
		return 2
	}

	var result analyzer.Result
	if flags.NArg() != 1 {
		result = analyzer.Result{
			Packages: []analyzer.Package{},
			Usages:   []analyzer.Usage{},
			LoadingErrors: []analyzer.LoadingError{{
				Kind:    "argument",
				Message: "exactly one repository path is required",
			}},
		}
	} else {
		result = analyzer.Analyze(flags.Arg(0))
	}

	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fmt.Fprintf(stderr, "encode analyzer result: %v\n", err)
		return 1
	}
	if len(result.LoadingErrors) > 0 {
		return 1
	}
	return 0
}
