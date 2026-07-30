package one

import "fmt"

// Greeting returns a formatted greeting.
func Greeting(name string) string {
	return fmt.Sprintf("hello, %s", name)
}
