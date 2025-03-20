import os
import re
import json
from typing import Any, Dict, List, Optional

from app.utils.vector_store import get_vector_store
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain.schema import Document


class RAGEngine:
    def __init__(self):
        self.vector_store = get_vector_store()
        self.llm = ChatOpenAI(
            model_name=os.getenv("CHAT_MODEL_NAME", "gpt-3.5-turbo"),
            temperature=0.7,
            openai_api_key=os.getenv("OPENAI_API_KEY"),
        )
        self.qa_prompt = PromptTemplate(
            template="""你是一個有幫助的AI助手。使用以下上下文來回答問題。
            
            上下文: {context}
            
            問題: {question}
            
            如果你找不到答案，請直接說不知道，不要試圖捏造答案。
            """,
            input_variables=["context", "question"],
        )
        
        # 新增產品相關問題的專用提示
        self.product_qa_prompt = PromptTemplate(
            template="""你是一個產品信息專家。請使用以下上下文回答關於產品的問題。
            
            上下文: {context}
            
            問題: {question}
            
            請盡可能詳細地回答產品相關問題，包括產品名稱、描述、價格、類別和規格等信息。
            如果找不到特定信息，請明確指出哪些信息是可用的，哪些信息不可用。
            """,
            input_variables=["context", "question"],
        )

    def setup_retrieval_qa(self, is_product_query=False):
        # 設置檢索問答系統
        retriever = self.vector_store.as_retriever(
            search_type="similarity", search_kwargs={"k": 5}
        )
        
        # 根據查詢類型選擇不同的提示模板
        prompt = self.product_qa_prompt if is_product_query else self.qa_prompt

        return RetrievalQA.from_chain_type(
            llm=self.llm,
            chain_type="stuff",
            retriever=retriever,
            return_source_documents=True,
            chain_type_kwargs={"prompt": prompt},
        )

    async def query(
        self, question: str, history: List[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        # 判斷是否是產品查詢
        is_product_query = self.is_product_query(question)
        
        qa = self.setup_retrieval_qa(is_product_query=is_product_query)
        result = await qa.acall({"query": question})

        answer = result["result"]
        sources = []

        # 提取來源文件信息
        if "source_documents" in result:
            for doc in result["source_documents"]:
                sources.append({"content": doc.page_content, "metadata": doc.metadata})

        return {"answer": answer, "sources": sources}
    
    def is_product_query(self, query: str) -> bool:
        """判斷是否是產品相關查詢"""
        # 檢查是否包含產品ID模式 (如HK-2189, TL-4523等)
        product_id_pattern = r'[A-Z]{2}-\d{4}'
        if re.search(product_id_pattern, query):
            return True
            
        # 檢查是否包含產品相關關鍵詞
        product_keywords = ["產品", "商品", "規格", "價格", "類別", "工作燈", "頭燈"]
        for keyword in product_keywords:
            if keyword in query:
                return True
                
        return False
        
    def get_product_by_id(self, product_id: str):
        """根據產品ID直接檢索相關文檔"""
        # 使用metadata過濾方式優先查詢
        try:
            # 嘗試使用metadata過濾
            docs = self.vector_store.get(
                where={"product_id": product_id}
            )
            if docs and len(docs) > 0:
                # 確保返回的是Document對象
                if not all(hasattr(doc, 'page_content') for doc in docs):
                    # 如果不是Document對象，轉換它們
                    docs = [
                        Document(page_content=doc if isinstance(doc, str) else str(doc),
                                 metadata={"source": "product_data", "product_id": product_id})
                        for doc in docs
                    ]
                return docs
        except Exception as e:
            print(f"使用metadata過濾查詢產品ID時出錯: {str(e)}")
        
        # 如果metadata過濾失敗，使用文本搜索
        docs = self.vector_store.similarity_search(product_id, k=3)
        
        # 確保返回的是Document對象
        if not all(hasattr(doc, 'page_content') for doc in docs):
            # 如果不是Document對象，轉換它們
            docs = [
                Document(page_content=doc if isinstance(doc, str) else str(doc),
                         metadata={"source": "similarity_search", "product_id": product_id})
                for doc in docs
            ]
        
        return docs

    def process_query(self, query, history=None):
        try:
            # 確保向量存儲已初始化
            if not self.vector_store:
                print("重新初始化向量存儲...")
                self.vector_store = get_vector_store()
                if not self.vector_store:
                    return {
                        "answer": "我沒有找到任何相關信息，可能是因為尚未上傳任何文件。",
                        "sources": [],
                    }

            # 使用向量搜索找出相關內容
            results = self.vector_store.similarity_search_with_score(
                query,
                k=3
            )

            # 整理搜索結果
            sources = []
            context = ""
            for doc, score in results:
                # 從內容中提取頁碼信息
                page_info = ""
                if "(第" in doc.page_content:
                    # 從內容中提取頁碼
                    page_matches = re.findall(r'第(\d+)頁', doc.page_content)
                    if page_matches:
                        page_info = f"(第 {page_matches[0]} 頁)"
                
                source = {
                    "content": doc.page_content,
                    "metadata": doc.metadata,
                    "score": score,
                    "page_info": page_info
                }
                sources.append(source)
                context += doc.page_content + "\n\n"

            # 如果找到相關內容，使用 GPT-4o 解析
            if sources:
                prompt = f"""基於以下產品目錄的內容：

{context}

請回答這個問題：{query}

請提供準確且完整的回答。如果是詢問特定產品，請包含所有相關的產品資訊。"""

                response = self.llm.invoke(prompt)
                
                # 從 AIMessage 對象中提取純文本內容
                answer = response.content if hasattr(response, 'content') else str(response)
                
                return {
                    "answer": answer,  # 現在是純字符串
                    "sources": sources
                }
            else:
                return {
                    "answer": "抱歉，我找不到相關的產品資訊。",
                    "sources": []
                }
            
        except Exception as e:
            print(f"處理查詢時出錯: {str(e)}")
            return {
                "answer": "處理查詢時發生錯誤。",
                "sources": []
            }

    def generate_response(self, query, docs, is_product_query=False):
        """根據查詢和文檔生成回答與來源"""
        try:
            # 如果沒有找到相關文檔
            if not docs or len(docs) == 0:
                return (
                    "我沒有找到與您問題相關的信息。請嘗試上傳更多相關文檔或重新表述您的問題。",
                    [],
                )

            # 準備上下文
            context_parts = []
            for doc in docs:
                # 檢查 doc 是否為 Document 對象或字符串
                if hasattr(doc, 'page_content'):
                    # 是 Document 對象
                    context_parts.append(doc.page_content)
                elif isinstance(doc, str):
                    # 是字符串
                    context_parts.append(doc)
                else:
                    # 其他類型
                    context_parts.append(str(doc))
            
            context = "\n\n".join(context_parts)

            # 創建提示，根據查詢類型選擇不同的提示模板
            prompt_template = self.product_qa_prompt if is_product_query else self.qa_prompt
            prompt = prompt_template.format(context=context, question=query)

            # 使用LLM生成回答
            messages = [
                {
                    "role": "system",
                    "content": "你是一個有幫助的助手，基於給定的上下文回答問題。" if not is_product_query else 
                              "你是一個產品信息專家，詳細解析產品信息並回答問題。",
                },
                {"role": "user", "content": prompt},
            ]

            # 調用 OpenAI API
            response = self.llm.invoke(messages)
            answer = response.content if hasattr(response, "content") else str(response)

            # 處理圖片信息
            sources = []
            for doc in docs:
                if hasattr(doc, 'metadata'):
                    source_info = {
                        "content": doc.page_content,
                        "source": doc.metadata.get("source", ""),
                        "images": {}
                    }
                    
                    # 解析圖片信息
                    images_str = doc.metadata.get("images", "{}")
                    try:
                        images_dict = json.loads(images_str)
                        for key, value in images_dict.items():
                            path, page = value.split("|")
                            source_info["images"][key] = {
                                "path": path,
                                "page": int(page)
                            }
                    except json.JSONDecodeError:
                        print(f"解析圖片信息時出錯: {images_str}")
                    
                    sources.append(source_info)
            
            return answer, sources

        except Exception as e:
            print(f"生成回答時出錯: {str(e)}")
            raise
