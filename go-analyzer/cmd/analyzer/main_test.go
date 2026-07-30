package main

import (
	"bytes"
	"encoding/json"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"code-migration-agent/go-analyzer/analyzer"
)

func TestRunRequiresRepositoryArgument(t *testing.T) {
	var stdout, stderr bytes.Buffer
	exitCode := run(nil, &stdout, &stderr)

	if exitCode != 1 {
		t.Errorf("run() exit code = %d, want 1", exitCode)
	}
	var result analyzer.Result
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("decode output: %v\noutput: %s", err, stdout.String())
	}
	if len(result.LoadingErrors) != 1 || result.LoadingErrors[0].Kind != "argument" {
		t.Errorf("LoadingErrors = %#v, want argument error", result.LoadingErrors)
	}
}

func TestCLIAnalyzesRepository(t *testing.T) {
	executable := filepath.Join(t.TempDir(), "analyzer")
	if runtime.GOOS == "windows" {
		executable += ".exe"
	}
	build := exec.Command("go", "build", "-o", executable, ".")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build CLI: %v\n%s", err, output)
	}

	fixture, err := filepath.Abs(filepath.Join("..", "..", "analyzer", "testdata", "multiple-packages"))
	if err != nil {
		t.Fatalf("resolve fixture: %v", err)
	}
	command := exec.Command(executable, fixture)
	output, err := command.Output()
	if err != nil {
		t.Fatalf("run CLI: %v", err)
	}

	var result analyzer.Result
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatalf("decode output: %v\noutput: %s", err, output)
	}
	if result.ModulePath != "example.com/multiple" || len(result.Packages) != 2 {
		t.Errorf("result = %#v", result)
	}
}
