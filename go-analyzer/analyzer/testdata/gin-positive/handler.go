package positive

import "github.com/gin-gonic/gin"

func handler(c *gin.Context) {
	c.Param("id")
	c.Query("q")
	c.ShouldBindJSON(&struct{}{})
	c.JSON(200, gin.H{"ok": true})
}

func routes() {
	first := gin.Default()
	second := gin.New()
	var middleware gin.HandlerFunc = handler
	first.Use(middleware)
	first.GET("/", handler)
	first.POST("/", handler)
	first.PUT("/", handler)
	first.DELETE("/", handler)
	group := second.Group("/group")
	group.Use(middleware)
	group.GET("/", handler)
	group.POST("/", handler)
	group.PUT("/", handler)
	group.DELETE("/", handler)
	group.Group("/nested")
}
