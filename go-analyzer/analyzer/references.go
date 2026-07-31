package analyzer

import (
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/tools/go/packages"
)

func findReferences(root string, pkg *packages.Package) []Reference {
	modules := packageModules(pkg)
	var references []Reference
	for _, file := range pkg.Syntax {
		ast.Inspect(file, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok {
				return true
			}
			enclosing := enclosingFunction(file, call.Pos(), pkg.TypesInfo)
			if reference := referenceForExpr(
				root, pkg, call.Fun, enclosing, modules,
			); reference.Symbol != "" {
				references = append(references, reference)
			}
			for _, argument := range call.Args {
				switch argument := argument.(type) {
				case *ast.Ident:
					if reference := referenceForExpr(
						root, pkg, argument, enclosing, modules,
					); reference.Symbol != "" {
						references = append(references, reference)
					}
				case *ast.SelectorExpr:
					if reference := referenceForExpr(
						root, pkg, argument, enclosing, modules,
					); reference.Symbol != "" {
						references = append(references, reference)
					}
				}
			}
			return true
		})
	}
	return deduplicateReferences(references)
}

func referenceForExpr(
	root string,
	pkg *packages.Package,
	expression ast.Expr,
	enclosing string,
	modules map[string]string,
) Reference {
	position := pkg.Fset.Position(expression.Pos())
	file := relativeFile(root, position.Filename)
	object, display := referencedObject(pkg.TypesInfo, expression)
	reference := Reference{
		File:            file,
		Line:            position.Line,
		Column:          position.Column,
		EnclosingSymbol: enclosing,
		Symbol:          display,
	}
	if object == nil {
		return reference
	}
	if _, ok := object.(*types.Func); !ok {
		return Reference{}
	}
	reference.Resolved = true
	reference.Signature = types.TypeString(object.Type(), packageQualifier)
	reference.FrameworkBound = strings.Contains(reference.Signature, ginImportPath)
	if object.Pkg() != nil {
		reference.PackagePath = object.Pkg().Path()
		reference.ModulePath = modules[reference.PackagePath]
		reference.StandardLibrary = !strings.Contains(reference.PackagePath, ".")
		reference.FrameworkBound = reference.FrameworkBound ||
			reference.PackagePath == ginImportPath
		if !strings.Contains(display, ".(*") {
			reference.Symbol = reference.PackagePath + "." + object.Name()
		}
	}
	definition := pkg.Fset.Position(object.Pos())
	if definition.IsValid() {
		reference.DefinitionFile = relativeFile(root, definition.Filename)
		reference.DefinitionLine = definition.Line
	}
	if function, ok := object.(*types.Func); ok && localFunctionUsesGin(pkg, function) {
		reference.FrameworkBound = true
	}
	return reference
}

func referencedObject(info *types.Info, expression ast.Expr) (types.Object, string) {
	switch expression := expression.(type) {
	case *ast.Ident:
		return info.Uses[expression], expression.Name
	case *ast.SelectorExpr:
		if selection := info.Selections[expression]; selection != nil {
			name, path := namedType(selection.Recv())
			if name != "" && path != "" {
				return selection.Obj(), path + ".(*" + name + ")." +
					expression.Sel.Name
			}
			return selection.Obj(), expression.Sel.Name
		}
		return info.Uses[expression.Sel], expression.Sel.Name
	default:
		return nil, types.ExprString(expression)
	}
}

func enclosingFunction(file *ast.File, position token.Pos, info *types.Info) string {
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || position < function.Pos() || position > function.End() {
			continue
		}
		object := info.Defs[function.Name]
		if object == nil || object.Pkg() == nil {
			return function.Name.Name
		}
		return object.Pkg().Path() + "." + object.Name()
	}
	return ""
}

func localFunctionUsesGin(pkg *packages.Package, function *types.Func) bool {
	for _, file := range pkg.Syntax {
		for _, declaration := range file.Decls {
			node, ok := declaration.(*ast.FuncDecl)
			if !ok || pkg.TypesInfo.Defs[node.Name] != function || node.Body == nil {
				continue
			}
			bound := false
			ast.Inspect(node.Body, func(candidate ast.Node) bool {
				selector, ok := candidate.(*ast.SelectorExpr)
				if !ok {
					return true
				}
				if selection := pkg.TypesInfo.Selections[selector]; selection != nil {
					_, path := namedType(selection.Recv())
					bound = bound || path == ginImportPath
				} else if object := pkg.TypesInfo.Uses[selector.Sel]; object != nil &&
					object.Pkg() != nil && object.Pkg().Path() == ginImportPath {
					bound = true
				}
				return !bound
			})
			return bound
		}
	}
	return false
}

func packageModules(root *packages.Package) map[string]string {
	result := map[string]string{}
	seen := map[string]bool{}
	var visit func(*packages.Package)
	visit = func(pkg *packages.Package) {
		if pkg == nil || seen[pkg.PkgPath] {
			return
		}
		seen[pkg.PkgPath] = true
		if pkg.Module != nil {
			result[pkg.PkgPath] = pkg.Module.Path
		}
		for _, imported := range pkg.Imports {
			visit(imported)
		}
	}
	visit(root)
	return result
}

func packageQualifier(pkg *types.Package) string {
	if pkg == nil {
		return ""
	}
	return pkg.Path()
}

func relativeFile(root, filename string) string {
	if relative, err := filepath.Rel(root, filename); err == nil {
		return filepath.ToSlash(relative)
	}
	return filepath.ToSlash(filename)
}

func deduplicateReferences(references []Reference) []Reference {
	seen := map[string]bool{}
	result := make([]Reference, 0, len(references))
	for _, reference := range references {
		key := strings.Join([]string{
			reference.File,
			strconv.Itoa(reference.Line),
			strconv.Itoa(reference.Column),
			reference.Symbol,
		}, ":")
		if seen[key] {
			continue
		}
		seen[key] = true
		result = append(result, reference)
	}
	return result
}
