// Package analyzer loads and describes Go repositories using Go's semantic
// package-loading APIs.
package analyzer

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"golang.org/x/tools/go/packages"
)

// Result is the JSON-serializable description of a Go repository.
type Result struct {
	ModulePath    string         `json:"module_path"`
	Packages      []Package      `json:"packages"`
	Usages        []Usage        `json:"usages"`
	References    []Reference    `json:"references"`
	LoadingErrors []LoadingError `json:"loading_errors"`
}

// Reference describes a semantically resolved symbol referenced by source.
type Reference struct {
	File             string   `json:"file"`
	Line             int      `json:"line"`
	Column           int      `json:"column"`
	EnclosingSymbol  string   `json:"enclosing_symbol,omitempty"`
	Symbol           string   `json:"symbol"`
	PackagePath      string   `json:"package_path,omitempty"`
	ModulePath       string   `json:"module_path,omitempty"`
	DefinitionFile   string   `json:"definition_file,omitempty"`
	DefinitionLine   int      `json:"definition_line,omitempty"`
	Signature        string   `json:"signature,omitempty"`
	FrameworkBound   bool     `json:"framework_bound"`
	StandardLibrary  bool     `json:"standard_library"`
	Resolved         bool     `json:"resolved"`
	ReceiverRootType string   `json:"receiver_root_type,omitempty"`
	ReceiverPath     []string `json:"receiver_path,omitempty"`
}

// Usage identifies one semantically resolved Gin usage.
type Usage struct {
	File   string `json:"file"`
	Line   int    `json:"line"`
	Column int    `json:"column"`
	Kind   string `json:"kind"`
	Symbol string `json:"symbol"`
}

// Package describes one package found below the repository root.
type Package struct {
	ImportPath  string   `json:"import_path"`
	Name        string   `json:"name"`
	SourceFiles []string `json:"source_files"`
	Imports     []string `json:"imports"`
}

// LoadingError is a structured repository or package loading failure.
type LoadingError struct {
	Kind     string `json:"kind"`
	Package  string `json:"package,omitempty"`
	Position string `json:"position,omitempty"`
	Message  string `json:"message"`
}

// Analyze loads all packages in repositoryPath and returns their semantic
// package metadata. Load and type-checking failures are included in Result.
func Analyze(repositoryPath string) Result {
	result := Result{
		Packages:      []Package{},
		Usages:        []Usage{},
		References:    []Reference{},
		LoadingErrors: []LoadingError{},
	}

	root, err := filepath.Abs(repositoryPath)
	if err != nil {
		result.LoadingErrors = append(result.LoadingErrors, LoadingError{
			Kind: "repository", Message: fmt.Sprintf("resolve repository path: %v", err),
		})
		return result
	}

	info, err := os.Stat(root)
	if err != nil {
		result.LoadingErrors = append(result.LoadingErrors, LoadingError{
			Kind: "repository", Message: fmt.Sprintf("access repository: %v", err),
		})
		return result
	}
	if !info.IsDir() {
		result.LoadingErrors = append(result.LoadingErrors, LoadingError{
			Kind: "repository", Message: "repository path is not a directory",
		})
		return result
	}

	cfg := &packages.Config{
		Dir: root,
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedDeps |
			packages.NeedSyntax |
			packages.NeedTypes |
			packages.NeedTypesInfo |
			packages.NeedModule,
	}
	loaded, err := packages.Load(cfg, "./...")
	if err != nil {
		result.LoadingErrors = append(result.LoadingErrors, LoadingError{
			Kind: "load", Message: err.Error(),
		})
		return result
	}

	for _, pkg := range loaded {
		if result.ModulePath == "" && pkg.Module != nil {
			result.ModulePath = pkg.Module.Path
		}

		files := append([]string(nil), pkg.GoFiles...)
		sort.Strings(files)

		imports := make([]string, 0, len(pkg.Imports))
		for path := range pkg.Imports {
			imports = append(imports, path)
		}
		sort.Strings(imports)

		result.Packages = append(result.Packages, Package{
			ImportPath:  pkg.PkgPath,
			Name:        pkg.Name,
			SourceFiles: files,
			Imports:     imports,
		})
		result.Usages = append(result.Usages, findGinUsages(root, pkg)...)
		result.References = append(result.References, findReferences(root, pkg)...)

		for _, loadErr := range pkg.Errors {
			result.LoadingErrors = append(result.LoadingErrors, LoadingError{
				Kind:     errorKind(loadErr.Kind),
				Package:  pkg.PkgPath,
				Position: loadErr.Pos,
				Message:  loadErr.Msg,
			})
		}
	}

	sort.Slice(result.Packages, func(i, j int) bool {
		return result.Packages[i].ImportPath < result.Packages[j].ImportPath
	})
	sort.SliceStable(result.LoadingErrors, func(i, j int) bool {
		left, right := result.LoadingErrors[i], result.LoadingErrors[j]
		if left.Package != right.Package {
			return left.Package < right.Package
		}
		if left.Position != right.Position {
			return left.Position < right.Position
		}
		return left.Message < right.Message
	})
	sort.Slice(result.Usages, func(i, j int) bool {
		left, right := result.Usages[i], result.Usages[j]
		if left.File != right.File {
			return left.File < right.File
		}
		if left.Line != right.Line {
			return left.Line < right.Line
		}
		if left.Column != right.Column {
			return left.Column < right.Column
		}
		if left.Kind != right.Kind {
			return left.Kind < right.Kind
		}
		return left.Symbol < right.Symbol
	})
	sort.Slice(result.References, func(i, j int) bool {
		left, right := result.References[i], result.References[j]
		if left.File != right.File {
			return left.File < right.File
		}
		if left.Line != right.Line {
			return left.Line < right.Line
		}
		if left.Column != right.Column {
			return left.Column < right.Column
		}
		return left.Symbol < right.Symbol
	})

	return result
}

func errorKind(kind packages.ErrorKind) string {
	switch kind {
	case packages.ListError:
		return "list"
	case packages.ParseError:
		return "parse"
	case packages.TypeError:
		return "type"
	default:
		return "unknown"
	}
}
