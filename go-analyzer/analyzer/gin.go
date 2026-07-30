package analyzer

import (
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"

	"golang.org/x/tools/go/packages"
)

const ginImportPath = "github.com/gin-gonic/gin"

var (
	ginPackageSymbols = map[string]string{
		"Default":     "constructor",
		"New":         "constructor",
		"Context":     "gin_type",
		"HandlerFunc": "gin_type",
		"H":           "gin_h",
	}
	ginRouteMethods = map[string]bool{
		"GET": true, "POST": true, "PUT": true, "DELETE": true,
	}
	ginContextMethods = map[string]bool{
		"Param": true, "Query": true, "ShouldBindJSON": true, "JSON": true,
	}
)

func findGinUsages(root string, pkg *packages.Package) []Usage {
	var usages []Usage
	for _, file := range pkg.Syntax {
		ast.Inspect(file, func(node ast.Node) bool {
			switch node := node.(type) {
			case *ast.ImportSpec:
				if importedPackagePath(pkg.TypesInfo, node) == ginImportPath {
					usages = append(usages, newUsage(root, pkg.Fset, node.Pos(), "gin_import", ginImportPath))
				}
			case *ast.SelectorExpr:
				if usage, ok := ginSelectorUsage(root, pkg, node); ok {
					usages = append(usages, usage)
				}
			}
			return true
		})
	}
	return usages
}

func importedPackagePath(info *types.Info, spec *ast.ImportSpec) string {
	var object types.Object
	if spec.Name != nil {
		object = info.Defs[spec.Name]
	} else {
		object = info.Implicits[spec]
	}
	pkgName, ok := object.(*types.PkgName)
	if !ok || pkgName.Imported() == nil {
		return ""
	}
	return pkgName.Imported().Path()
}

func ginSelectorUsage(root string, pkg *packages.Package, selector *ast.SelectorExpr) (Usage, bool) {
	if selection := pkg.TypesInfo.Selections[selector]; selection != nil {
		receiverName, receiverPath := namedType(selection.Recv())
		if receiverPath != ginImportPath {
			return Usage{}, false
		}

		method := selector.Sel.Name
		kind := ""
		switch {
		case receiverName == "Context" && ginContextMethods[method]:
			kind = "context_method"
		case (receiverName == "Engine" || receiverName == "RouterGroup") && ginRouteMethods[method]:
			kind = routeUsageKind(receiverName)
		case (receiverName == "Engine" || receiverName == "RouterGroup") && method == "Group":
			kind = "group"
		case (receiverName == "Engine" || receiverName == "RouterGroup") && method == "Use":
			kind = "middleware"
		default:
			return Usage{}, false
		}

		symbol := ginImportPath + ".(*" + receiverName + ")." + method
		return newUsage(root, pkg.Fset, selector.Pos(), kind, symbol), true
	}

	object := pkg.TypesInfo.Uses[selector.Sel]
	if object == nil || object.Pkg() == nil || object.Pkg().Path() != ginImportPath {
		return Usage{}, false
	}
	kind, ok := ginPackageSymbols[object.Name()]
	if !ok {
		return Usage{}, false
	}
	symbol := ginImportPath + "." + object.Name()
	return newUsage(root, pkg.Fset, selector.Pos(), kind, symbol), true
}

func namedType(typ types.Type) (name, packagePath string) {
	if pointer, ok := types.Unalias(typ).(*types.Pointer); ok {
		typ = pointer.Elem()
	}
	named, ok := types.Unalias(typ).(*types.Named)
	if !ok || named.Obj().Pkg() == nil {
		return "", ""
	}
	return named.Obj().Name(), named.Obj().Pkg().Path()
}

func routeUsageKind(receiver string) string {
	if receiver == "Engine" {
		return "engine_method"
	}
	return "router_group_method"
}

func newUsage(root string, fileSet *token.FileSet, position token.Pos, kind, symbol string) Usage {
	location := fileSet.Position(position)
	file := location.Filename
	if relative, err := filepath.Rel(root, file); err == nil {
		file = relative
	}
	return Usage{
		File:   filepath.ToSlash(file),
		Line:   location.Line,
		Column: location.Column,
		Kind:   kind,
		Symbol: symbol,
	}
}
