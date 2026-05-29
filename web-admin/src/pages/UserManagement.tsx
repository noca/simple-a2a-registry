import React,{useEffect,useState,useCallback} from 'react';
import {Card,Table,Button,Input,Select,Form,Modal,message,Spin,Empty,Typography,Space,Switch,Tag,Row,Col} from 'antd';
import {PlusOutlined,ReloadOutlined,DeleteOutlined,SearchOutlined} from '@ant-design/icons';
import {listUsers,createUser,deleteUser,toggleUser} from '../api/client';
const{Title,Text}=Typography;
const UserManagement:React.FC=()=>{
  const[us,setUs]=useState<any[]>([]);const[lo,setLo]=useState(true);const[sr,setSr]=useState('');
  const[co,setCo]=useState(false);const[f]=Form.useForm();
  const fetch=useCallback(async()=>{
    setLo(true);try{const d=await listUsers({limit:200,...(sr?{q:sr}:{})});setUs(Array.isArray(d)?d:(d.users||[]));}catch(e){console.warn('[Users] fetch failed',e)}finally{setLo(false);}
  },[sr]);
  useEffect(()=>{fetch();},[fetch]);
  const create=async(v:any)=>{try{await createUser(v);message.success('Created');setCo(false);f.resetFields();fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  const del=(id:string)=>Modal.confirm({title:'Delete User',content:'This cannot be undone.',okText:'Delete',okType:'danger',onOk:async()=>{try{await deleteUser(id);message.success('Deleted');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}}});
  const tog=async(id:string)=>{try{const r=await toggleUser(id);message.success(r.disabled?'Disabled':'Enabled');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  return React.createElement('div',null,
    React.createElement('div',{style:{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:20}},
      React.createElement(Title,{level:3,style:{margin:0,fontWeight:600,fontSize:22}},'Users',React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},`${us.length} total`)),
      React.createElement(Button,{type:'primary',icon:React.createElement(PlusOutlined),onClick:()=>setCo(true)},'Add User')),
    React.createElement(Card,{bodyStyle:{padding:'12px 16px'},style:{marginBottom:16,borderRadius:10}},
      React.createElement(Row,{gutter:[12,12],align:'middle'},
        React.createElement(Col,{xs:18,sm:12,md:8},React.createElement(Input,{prefix:React.createElement(SearchOutlined,{style:{color:'var(--text-tertiary)'}}),placeholder:'Search users...',value:sr,onChange:(e:any)=>setSr(e.target.value),allowClear:true})),
        React.createElement(Col,null,React.createElement(Button,{icon:React.createElement(ReloadOutlined),onClick:fetch},'Refresh')))),
    React.createElement(Spin,{spinning:lo},us.length===0&&!lo?React.createElement(Card,{style:{borderRadius:10,textAlign:'center',padding:40}},React.createElement(Empty,{description:'No users'})):
      React.createElement(Card,{bodyStyle:{padding:0},style:{borderRadius:10}},
        React.createElement(Table,{dataSource:us,columns:[{title:'Username',dataIndex:'username',render:(u:string)=>React.createElement(Text,{style:{fontWeight:500}},u)},{title:'Role',dataIndex:'role',render:(r:string)=>React.createElement(Tag,{color:r==='admin'?'red':'blue',style:{borderRadius:4}},r||'user')},{title:'Email',dataIndex:'email',render:(e:string)=>e||'-'},{title:'Avatar',dataIndex:'avatar_url',render:(a:string)=>a?React.createElement('img',{src:a,style:{width:28,height:28,borderRadius:'50%',objectFit:'cover'}}):'-'},{title:'Created',dataIndex:'created_at',render:(t:number)=>t?new Date(t*1000).toLocaleDateString():'-'},{title:'Enabled',render:(_:any,r:any)=>React.createElement(Switch,{size:'small',checked:!r.disabled,onChange:()=>tog(r.id)})},{title:'Actions',render:(_:any,r:any)=>React.createElement(Space,null,React.createElement(Button,{size:'small',danger:true,icon:React.createElement(DeleteOutlined),onClick:()=>del(r.id)}))}],rowKey:'id',pagination:{pageSize:20,showTotal:(t:number)=>`${t} users`},size:'middle',scroll:{x:800}}))),
    React.createElement(Modal,{title:'Add User',open:co,onCancel:()=>{setCo(false);f.resetFields();},onOk:()=>f.submit(),okText:'Create'},
      React.createElement(Form,{form:f,layout:'vertical',onFinish:create},
        React.createElement(Form.Item,{name:'username',label:'Username',rules:[{required:true}]},React.createElement(Input,{placeholder:'john'})),
        React.createElement(Form.Item,{name:'password',label:'Password',rules:[{required:true}]},React.createElement(Input.Password,null)),
        React.createElement(Form.Item,{name:'role',label:'Role'},React.createElement(Select,{defaultValue:'user'},React.createElement(Select.Option,{value:'user',children:'User'}),React.createElement(Select.Option,{value:'admin',children:'Admin'}))),
        React.createElement(Form.Item,{name:'email',label:'Email'},React.createElement(Input,{type:'email',placeholder:'user@example.com'})))));
};
export default UserManagement;
