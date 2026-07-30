package analyzer

import (
	"fmt"
	"testing"
)

func TestAnalyzeGinUsages(t *testing.T) {
	result := Analyze(fixturePath(t, "gin-positive"))
	if len(result.LoadingErrors) != 0 {
		t.Fatalf("Analyze() errors = %#v", result.LoadingErrors)
	}

	got := make(map[string]int)
	for _, usage := range result.Usages {
		key := fmt.Sprintf("%s|%s", usage.Kind, usage.Symbol)
		got[key]++
		if usage.File != "handler.go" {
			t.Errorf("usage file = %q, want handler.go", usage.File)
		}
		if usage.Line < 1 || usage.Column < 1 {
			t.Errorf("invalid usage position: %#v", usage)
		}
	}

	want := map[string]int{
		"gin_import|github.com/gin-gonic/gin":                                1,
		"constructor|github.com/gin-gonic/gin.Default":                       1,
		"constructor|github.com/gin-gonic/gin.New":                           1,
		"gin_type|github.com/gin-gonic/gin.Context":                          1,
		"gin_type|github.com/gin-gonic/gin.HandlerFunc":                      1,
		"gin_h|github.com/gin-gonic/gin.H":                                   1,
		"engine_method|github.com/gin-gonic/gin.(*Engine).GET":               1,
		"engine_method|github.com/gin-gonic/gin.(*Engine).POST":              1,
		"engine_method|github.com/gin-gonic/gin.(*Engine).PUT":               1,
		"engine_method|github.com/gin-gonic/gin.(*Engine).DELETE":            1,
		"router_group_method|github.com/gin-gonic/gin.(*RouterGroup).GET":    1,
		"router_group_method|github.com/gin-gonic/gin.(*RouterGroup).POST":   1,
		"router_group_method|github.com/gin-gonic/gin.(*RouterGroup).PUT":    1,
		"router_group_method|github.com/gin-gonic/gin.(*RouterGroup).DELETE": 1,
		"group|github.com/gin-gonic/gin.(*Engine).Group":                     1,
		"group|github.com/gin-gonic/gin.(*RouterGroup).Group":                1,
		"middleware|github.com/gin-gonic/gin.(*Engine).Use":                  1,
		"middleware|github.com/gin-gonic/gin.(*RouterGroup).Use":             1,
		"context_method|github.com/gin-gonic/gin.(*Context).Param":           1,
		"context_method|github.com/gin-gonic/gin.(*Context).Query":           1,
		"context_method|github.com/gin-gonic/gin.(*Context).ShouldBindJSON":  1,
		"context_method|github.com/gin-gonic/gin.(*Context).JSON":            1,
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("Gin usages mismatch\ngot:  %v\nwant: %v", got, want)
	}
}

func TestAnalyzeDoesNotClassifyUnrelatedSymbolsAsGin(t *testing.T) {
	result := Analyze(fixturePath(t, "gin-negative"))
	if len(result.LoadingErrors) != 0 {
		t.Fatalf("Analyze() errors = %#v", result.LoadingErrors)
	}
	if len(result.Usages) != 0 {
		t.Fatalf("Usages = %#v, want none", result.Usages)
	}
}
