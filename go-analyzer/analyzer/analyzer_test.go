package analyzer

import (
	"path/filepath"
	"strings"
	"testing"
)

func fixturePath(t *testing.T, name string) string {
	t.Helper()
	path, err := filepath.Abs(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("resolve fixture path: %v", err)
	}
	return path
}

func TestAnalyzeOnePackage(t *testing.T) {
	result := Analyze(fixturePath(t, "one-package"))

	if len(result.LoadingErrors) != 0 {
		t.Fatalf("Analyze() errors = %#v", result.LoadingErrors)
	}
	if result.ModulePath != "example.com/one" {
		t.Errorf("ModulePath = %q, want example.com/one", result.ModulePath)
	}
	if len(result.Packages) != 1 {
		t.Fatalf("len(Packages) = %d, want 1", len(result.Packages))
	}
	pkg := result.Packages[0]
	if pkg.ImportPath != "example.com/one" || pkg.Name != "one" {
		t.Errorf("package = %#v", pkg)
	}
	if len(pkg.SourceFiles) != 1 || !filepath.IsAbs(pkg.SourceFiles[0]) {
		t.Errorf("SourceFiles = %#v, want one absolute path", pkg.SourceFiles)
	}
	if len(pkg.Imports) != 1 || pkg.Imports[0] != "fmt" {
		t.Errorf("Imports = %#v, want [fmt]", pkg.Imports)
	}
}

func TestAnalyzeMultiplePackages(t *testing.T) {
	result := Analyze(fixturePath(t, "multiple-packages"))

	if len(result.LoadingErrors) != 0 {
		t.Fatalf("Analyze() errors = %#v", result.LoadingErrors)
	}
	if len(result.Packages) != 2 {
		t.Fatalf("len(Packages) = %d, want 2", len(result.Packages))
	}
	got := []string{result.Packages[0].ImportPath, result.Packages[1].ImportPath}
	want := []string{"example.com/multiple", "example.com/multiple/greeting"}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("package path %d = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestAnalyzeInvalidGoCodeReturnsStructuredErrors(t *testing.T) {
	result := Analyze(fixturePath(t, "invalid"))

	if len(result.LoadingErrors) == 0 {
		t.Fatal("Analyze() returned no loading errors")
	}
	if result.LoadingErrors[0].Kind != "parse" {
		t.Errorf("first error kind = %q, want parse", result.LoadingErrors[0].Kind)
	}
	if result.LoadingErrors[0].Package != "example.com/invalid" {
		t.Errorf("first error package = %q", result.LoadingErrors[0].Package)
	}
	if !strings.Contains(result.LoadingErrors[0].Message, "expected") {
		t.Errorf("first error message = %q, want parser error", result.LoadingErrors[0].Message)
	}
}

func TestAnalyzeMissingRepositoryReturnsStructuredError(t *testing.T) {
	result := Analyze(filepath.Join(t.TempDir(), "missing"))

	if len(result.Packages) != 0 {
		t.Errorf("Packages = %#v, want empty", result.Packages)
	}
	if len(result.LoadingErrors) != 1 || result.LoadingErrors[0].Kind != "repository" {
		t.Fatalf("LoadingErrors = %#v, want one repository error", result.LoadingErrors)
	}
}
