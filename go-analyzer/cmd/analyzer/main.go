package main

import (
	"flag"
	"fmt"
)

func main() {
	flag.Usage = func() {
		fmt.Fprintln(flag.CommandLine.Output(), "Usage: analyzer [options]")
		flag.PrintDefaults()
	}
	flag.Parse()
}
